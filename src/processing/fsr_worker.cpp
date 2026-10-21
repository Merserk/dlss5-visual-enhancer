#include "fsr_worker.hpp"
#include "fsr_video.hpp"
#include "media/host_video.hpp"
#include "media/video_preview.hpp"
#include "infrastructure/paths.hpp"
#include "infrastructure/log.hpp"
#include "infrastructure/process.hpp"
#include "infrastructure/scratch.hpp"
#include <QCoreApplication>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QDir>
#include <QJsonDocument>
#include <QJsonArray>
#include <QSaveFile>
#include <QScopeGuard>
#include <QTemporaryFile>
#include <QTextStream>
#include <mutex>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <limits>
#include <thread>
#include <windows.h>
#include <tlhelp32.h>

namespace ve {
namespace {
std::mutex diagnosticMutex;QString diagnostics;
void send(const QJsonObject& message){QTextStream stream(stdout);stream<<QJsonDocument(message).toJson(QJsonDocument::Compact)<<'\n';stream.flush();}
void save(const QString& path,const QByteArray& bytes){QSaveFile file(path);if(!file.open(QIODevice::WriteOnly)||file.write(bytes)!=bytes.size()||!file.commit())throw std::runtime_error("Cannot save FSR worker request");}
QJsonArray auditModules(bool strict){
    QJsonArray modules;const auto snapshot=CreateToolhelp32Snapshot(TH32CS_SNAPMODULE,GetCurrentProcessId());
    if(snapshot==INVALID_HANDLE_VALUE)throw std::runtime_error("Cannot audit FSR worker modules");const auto close=qScopeGuard([&]{CloseHandle(snapshot);});
    MODULEENTRY32W module{};module.dwSize=sizeof(module);if(!Module32FirstW(snapshot,&module))throw std::runtime_error("Cannot enumerate FSR worker modules");
    do{const auto name=QString::fromWCharArray(module.szModule).toLower();
        if(strict&&(name.startsWith("nvcuda")||name.startsWith("cudart")||name.startsWith("cublas")||name.startsWith("nvofapi")||name.startsWith("nvopticalflow")))
            throw std::runtime_error("FSR-only worker loaded an unexpected CUDA or NVIDIA Optical Flow module: "+name.toStdString());
        if(name.contains("fidelityfx")||name.startsWith("avcodec")||name.startsWith("avutil")||name.startsWith("nvencode")||name.contains("amfrt")||name.contains("fsr_frame")||name.startsWith("libvpl")||name.startsWith("libmfx"))modules.append(QString::fromWCharArray(module.szExePath));
    }while(Module32NextW(snapshot,&module));return modules;
}
bool strictAudit(const QStringList& stages){
    for(const auto& stage:stages)if(QStringList{"neural_model","dlss_super_resolution","dlss_ray_reconstruction","super_resolution","rtx_video_hdr","reshade"}.contains(stage)||isVfxStage(stage))return false;
    return true;
}
}
QString fsrWorkerDiagnostics(){std::lock_guard lock(diagnosticMutex);return diagnostics;}
QJsonArray fsrAuditLoadedModules(bool strict){return auditModules(strict);}
RenderResult renderIsolatedFsr(const RenderRequest& request,JobControl& control,const ProgressCallback& progress){
    QElapsedTimer elapsed;elapsed.start();if(QFileInfo::exists(request.output))throw std::runtime_error("Output already exists. Choose a different filename.");
    ProcessingTemporaryDirectory directory(ProcessingCacheKind::Scratch);if(!directory.isValid())throw std::runtime_error("Cannot create FSR worker directory");
    const auto rendered=directory.filePath("processed."+QFileInfo(request.output).suffix()),preset=directory.filePath("preset.json"),file=directory.filePath("job.json");
    if(const auto result=request.settings.exportPreset(preset,"FSR worker");!result)throw std::runtime_error(result.error().toStdString());
    save(file,QJsonDocument(QJsonObject{{"protocol",1},{"input",request.input},{"output",rendered},{"preset",preset},{"workflow",int(request.workflow)},
        {"preview",request.preview},{"start",request.startSeconds},{"seconds",request.seconds},{"temporal_context",request.temporalContextIncluded}}).toJson());
    directory.cacheRoot()->registerFile(file+".state");
    const auto stateError=std::make_shared<std::atomic<bool>>(false);
    const auto writeState=[state=file+".state",error=stateError](bool paused,bool cancelled){try{save(state,cancelled?"C":paused?"P":"R");}catch(...){*error=true;}};
    writeState(control.paused(),control.cancelled());if(*stateError)throw std::runtime_error("Cannot create FSR worker control channel");
    control.setStateChanged(writeState);const auto clear=qScopeGuard([&]{control.setStateChanged({});});QByteArray pending;
    auto executable=qEnvironmentVariable("VE_FSR_WORKER_EXECUTABLE");if(executable.isEmpty())executable=QFileInfo(QCoreApplication::applicationFilePath()).fileName().compare("VE_CLI.exe",Qt::CaseInsensitive)==0?QCoreApplication::applicationFilePath():Paths::instance().at("VE_CLI.exe");
    const auto result=runProcess(executable,{"--internal-fsr-worker",file},&control,0,{},[&](const QByteArray& bytes){
        if(*stateError)throw std::runtime_error("Cannot update FSR worker pause/cancel state");pending+=bytes;
        for(auto end=pending.indexOf('\n');end>=0;end=pending.indexOf('\n')){const auto message=QJsonDocument::fromJson(pending.left(end)).object();pending.remove(0,end+1);
            if(message.value("kind")=="progress"&&progress)progress({std::min(.98,message.value("fraction").toDouble()),message.value("message").toString(),message.value("frames").toInteger(),message.value("total").toInteger(),message.value("fps").toDouble()});
            if(message.value("kind")=="diagnostics"){const auto text=message.value("text").toString();{std::lock_guard lock(diagnosticMutex);diagnostics=text;}info("fsr-worker",text);}
        }
    });
    if(result.exitCode)throw std::runtime_error(result.error.isEmpty()?"FSR worker exited unexpectedly":result.error.toStdString());
    QDir().mkpath(QFileInfo(request.output).absolutePath());QFile source(rendered);QTemporaryFile target(QFileInfo(request.output).absolutePath()+"/.fsr-delivery-XXXXXX");
    if(!source.open(QIODevice::ReadOnly)||!target.open())throw std::runtime_error("Cannot save FSR worker delivery");
    while(!source.atEnd()){control.checkpoint();const auto bytes=source.read(8*1024*1024);if(bytes.isEmpty()&&source.error()!=QFileDevice::NoError)throw std::runtime_error("Cannot read FSR worker delivery");if(target.write(bytes)!=bytes.size())throw std::runtime_error("Cannot write FSR worker delivery");}
    control.checkpoint();target.close();if(!target.rename(request.output))throw std::runtime_error("Cannot commit FSR worker output; destination may already exist");target.setAutoRemove(false);
    auto output=probeMedia(request.output,&control);
    const auto report=QJsonDocument::fromJson(fsrWorkerDiagnostics().toUtf8()).object();if(report.contains("source_time_origin"))output.metadata.insert("ve_source_time_origin",report.value("source_time_origin"));
    if(progress)progress({1,"Video complete",output.frames,output.frames});
    return{request.input,request.output,Log::instance().sessionPath(),output,elapsed.elapsed()/1000.};
}
QImage previewIsolatedFsr(const QString& path,const Settings& settings,const MediaInfo& info,double seconds,JobControl& control){
    ProcessingTemporaryDirectory directory(ProcessingCacheKind::Scratch);if(!directory.isValid())throw std::runtime_error("Cannot create FSR still preview directory");
    const auto stages=settings.stages(true);const auto start=reconstructionContextStart(path,info,seconds,reconstructionHistoryFrames(settings,stages),control,0,false);
    const auto duration=std::max(.25,3./std::max(1.,info.fps));
    RenderRequest request{path,directory.filePath("preview.mkv"),videoPreviewSettings(settings),Workflow::NeuralRendering,true,start,seconds-start+duration};request.temporalContextIncluded=true;
    const auto rendered=renderIsolatedFsr(request,control,{});const auto origin=rendered.info.metadata.value("ve_source_time_origin").toDouble(info.presentationStartSeconds()+start);
    const double target=std::max(0.,info.presentationStartSeconds()+seconds-origin);HostVideoDecoder decoder(rendered.output,rendered.info,0,0,false,control);
    std::optional<TimedImage> selected;double distance=std::numeric_limits<double>::infinity();
    while(auto frame=decoder.read()){control.checkpoint();const auto candidate=std::abs(frame->seconds()-target);if(candidate<distance){distance=candidate;selected=std::move(frame);}else if(frame->seconds()>target)break;}
    if(!selected)throw std::runtime_error("FSR still preview returned no selected frame");return selected->image;
}
int runFsrWorker(const QString& path){
    try{
        ProcessingCacheActivity activity;QFile file(path);if(!file.open(QIODevice::ReadOnly))throw std::runtime_error("Cannot read FSR worker request");
        const auto job=QJsonDocument::fromJson(file.readAll()).object();if(job.value("protocol").toInt()!=1)throw std::runtime_error("Invalid FSR worker protocol");
        WorkerScratchScope scratch(QFileInfo(path).absolutePath());Settings settings;if(const auto imported=settings.importPreset(job.value("preset").toString());!imported)throw std::runtime_error(imported.error().toStdString());
        JobControl control;QElapsedTimer poll;poll.start();control.setExternalCheckpoint([&]{
            if(poll.elapsed()<20)return;const auto started=std::chrono::steady_clock::now();bool paused=false;
            for(;;){QFile state(path+".state");if(!state.open(QIODevice::ReadOnly))throw Cancelled{};const auto value=state.readAll();state.close();if(value=="C")throw Cancelled{};if(value!="P")break;paused=true;std::this_thread::sleep_for(std::chrono::milliseconds(20));}
            if(paused)control.accountExternalPause(std::chrono::steady_clock::now()-started);poll.restart();
        });
        const auto workflow=job.value("workflow").toInt();if(workflow<int(Workflow::NeuralRendering)||workflow>int(Workflow::FrameInterpolation))throw std::runtime_error("Invalid FSR worker workflow");
        RenderRequest request{job.value("input").toString(),job.value("output").toString(),settings,Workflow(workflow),job.value("preview").toBool(),job.value("start").toDouble(),job.value("seconds").toDouble()};
        request.isolatedFsrWorker=true;request.temporalContextIncluded=job.value("temporal_context").toBool();
        const auto active=workflowSettings(settings,request.workflow,true).stages(true);if(!active.contains("fsr_frame_generation"))throw std::runtime_error("Worker request has no FSR Frame Generation stage");
        const auto rendered=Pipeline{}.render(request,control,[](const Progress& p){send({{"kind","progress"},{"fraction",p.fraction},{"message",p.message},{"frames",p.frames},{"total",p.totalFrames},{"fps",p.fps}});});(void)rendered;
        auto report=QJsonDocument::fromJson(fsrVideoDiagnostics().toUtf8()).object();const auto strict=strictAudit(active);report.insert("remaining_modules",auditModules(strict));report.insert("cuda_free_worker",strict);
        send({{"kind","diagnostics"},{"text",QString::fromUtf8(QJsonDocument(report).toJson(QJsonDocument::Compact))}});return 0;
    }catch(const std::exception& error){send({{"kind","diagnostics"},{"text",fsrVideoDiagnostics()}});QTextStream(stderr)<<error.what()<<'\n';return 1;}
}
}
