#include "process.hpp"
#include <QElapsedTimer>
#include <algorithm>
#ifdef Q_OS_WIN
#include <windows.h>
#endif

namespace ve {
namespace {
void start(QProcess& process, const QString& program, const QStringList& arguments, void* outputHandle=nullptr) {
#ifdef Q_OS_WIN
    process.setCreateProcessArgumentsModifier([outputHandle](QProcess::CreateProcessArguments* arguments) {
        arguments->flags |= 0x08000000; // CREATE_NO_WINDOW: child tools never flash a console.
        arguments->startupInfo->dwFlags |= STARTF_FORCEOFFFEEDBACK;
        if (outputHandle) {
            arguments->startupInfo->dwFlags |= STARTF_USESTDHANDLES;
            arguments->startupInfo->hStdOutput = outputHandle;
            arguments->inheritHandles = true;
        }
    });
#else
    Q_UNUSED(outputHandle);
#endif
    process.setProgram(program);
    process.setArguments(arguments);
    process.start();
    if (!process.waitForStarted(10000))
        throw std::runtime_error(process.errorString().toStdString());
}
} // namespace
ProcessResult runProcess(const QString& program, const QStringList& arguments, JobControl* control,
                         int timeoutMs, const QByteArray& input, const std::function<void(const QByteArray&)>& outputReady) {
    QProcess process;
    start(process, program, arguments);
    QElapsedTimer clock;
    clock.start();
    ProcessResult result;
    qsizetype written = 0;
    bool closed = false;
    try {
        while (process.state() != QProcess::NotRunning) {
            if (control)
                control->checkpoint();
            if (timeoutMs > 0 && clock.elapsed() > timeoutMs)
                throw std::runtime_error("External process timed out.");
            if (written < input.size() && process.bytesToWrite() < 1024 * 1024) {
                const auto amount = std::min<qsizetype>(1024 * 1024, input.size() - written);
                const auto accepted = process.write(input.constData() + written, amount);
                if (accepted < 0)
                    throw std::runtime_error("External process closed its input.");
                written += accepted;
            } else if (!closed && written == input.size()) {
                process.closeWriteChannel();
                closed = true;
            }
            process.waitForReadyRead(20);
            const auto output=process.readAllStandardOutput();if(outputReady&&!output.isEmpty())outputReady(output);result.output += output;
            if(outputReady&&result.output.size()>1024*1024)result.output=result.output.right(1024*1024);
            result.error += process.readAllStandardError();
            if (result.error.size() > 2 * 1024 * 1024)
                result.error = result.error.right(1024 * 1024);
        }
    } catch (...) {
        process.kill();
        process.waitForFinished(3000);
        throw;
    }
    const auto output=process.readAllStandardOutput();if(outputReady&&!output.isEmpty())outputReady(output);result.output += output;
    result.error += process.readAllStandardError();
    result.exitCode = process.exitStatus() == QProcess::NormalExit ? process.exitCode() : -1;
    return result;
}
struct PipeProcess::OutputPipe {
#ifdef Q_OS_WIN
    HANDLE reader=nullptr,writer=nullptr;
    OutputPipe() {
        SECURITY_ATTRIBUTES security{sizeof(SECURITY_ATTRIBUTES),nullptr,TRUE};
        if (!CreatePipe(&reader,&writer,&security,1024*1024))
            throw std::runtime_error("Cannot create bounded decoder output pipe.");
        if (!SetHandleInformation(reader,HANDLE_FLAG_INHERIT,0)) {
            CloseHandle(reader);CloseHandle(writer);reader=writer=nullptr;
            throw std::runtime_error("Cannot protect decoder pipe read handle.");
        }
    }
    void closeWriter() { if (writer) {CloseHandle(writer);writer=nullptr;} }
    ~OutputPipe() {closeWriter();if(reader)CloseHandle(reader);}
#endif
};
PipeProcess::PipeProcess(const QString& program, const QStringList& arguments, OutputMode outputMode) {
    void* outputHandle=nullptr;
#ifdef Q_OS_WIN
    if (outputMode==OutputMode::Backpressured) {
        outputPipe_=std::make_unique<OutputPipe>();
        outputHandle=outputPipe_->writer;
        // Avoid creating QWindowsPipeReader for stdout; its native callbacks
        // keep buffering even while the application decoder thread is paused.
        process_.setProcessChannelMode(QProcess::ForwardedOutputChannel);
    }
#else
    Q_UNUSED(outputMode);
#endif
    start(process_, program, arguments,outputHandle);
#ifdef Q_OS_WIN
    if(outputPipe_)outputPipe_->closeWriter(); // Only the child may keep stdout open.
#endif
}
PipeProcess::~PipeProcess() {
    if (process_.state() != QProcess::NotRunning) {
        process_.kill();
        process_.waitForFinished(3000);
    }
}
QByteArray PipeProcess::readSome(qsizetype maximum, JobControl& control) {
    while (true) {
        control.checkpoint();
        drainErrors();
        auto data = readOutput(maximum);
        if (!data.isEmpty())
            return data;
        if (process_.state() == QProcess::NotRunning) {
            if (process_.exitCode() != 0)
                throw std::runtime_error(error_.toStdString());
            return {};
        }
        if(outputPipe_)process_.waitForFinished(2);
        else process_.waitForReadyRead(25);
    }
}
QByteArray PipeProcess::readAvailable(qsizetype maximum, JobControl& control) {
    control.checkpoint();
    if(outputPipe_)process_.waitForFinished(0);
    else process_.waitForReadyRead(0);
    drainErrors();
    auto result=readOutput(maximum);
    if (result.isEmpty() && process_.state()==QProcess::NotRunning && process_.exitCode()!=0)
        throw std::runtime_error(error_.toStdString());
    return result;
}
QByteArray PipeProcess::readOutput(qsizetype maximum) {
#ifdef Q_OS_WIN
    if(outputPipe_) {
        if(maximum<=0)return {};
        DWORD available=0;
        if(!PeekNamedPipe(outputPipe_->reader,nullptr,0,nullptr,&available,nullptr)) {
            const auto code=GetLastError();
            if(code==ERROR_BROKEN_PIPE)return {};
            throw std::runtime_error("Cannot inspect decoder output pipe: Windows error "+std::to_string(code));
        }
        if(!available)return {};
        const auto amount=DWORD(std::min<qsizetype>({maximum,qsizetype(available),1024*1024}));
        QByteArray data(amount,Qt::Uninitialized);DWORD received=0;
        if(!ReadFile(outputPipe_->reader,data.data(),amount,&received,nullptr)) {
            const auto code=GetLastError();
            if(code==ERROR_BROKEN_PIPE)return {};
            throw std::runtime_error("Cannot read decoder output pipe: Windows error "+std::to_string(code));
        }
        data.resize(received);return data;
    }
#endif
    return process_.read(maximum);
}
void PipeProcess::drainErrors() {
    error_ += process_.readAllStandardError();
    if (error_.size() > 1024 * 1024)
        error_ = error_.right(512 * 1024);
}
QByteArray PipeProcess::read(qsizetype size, JobControl& control) {
    QByteArray data;
    data.reserve(size);
    while (data.size() < size) {
        const auto next=readSome(size-data.size(),control);
        if(next.isEmpty())break;
        data+=next;
    }
    if (!data.isEmpty() && data.size() != size)
        throw std::runtime_error("Decoder returned an incomplete video frame.");
    if (data.isEmpty() && process_.state() == QProcess::NotRunning && process_.exitCode() != 0)
        throw std::runtime_error(error_.toStdString());
    return data;
}
void PipeProcess::write(const char* data, qsizetype size, JobControl& control) {
    if(outputPipe_)throw std::logic_error("A backpressured decoder pipe is read-only.");
    qsizetype offset = 0;
    while (offset < size) {
        control.checkpoint();
        drainErrors();
        if (process_.state() == QProcess::NotRunning)
            throw std::runtime_error(error_.toStdString());
        if (process_.bytesToWrite() > 4 * 1024 * 1024) {
            // A filter writes while it reads. Pump both pipe directions so a
            // full stdout pipe cannot prevent it from consuming our input.
            process_.waitForBytesWritten(20);
            continue;
        }
        const auto accepted = process_.write(data + offset, std::min<qsizetype>(1024 * 1024, size - offset));
        if (accepted <= 0)
            throw std::runtime_error("Encoder closed its input: " + error_.toStdString());
        offset += accepted;
    }
    while (process_.bytesToWrite() > 0) {
        control.checkpoint();
        drainErrors();
        if (process_.state() == QProcess::NotRunning)
            throw std::runtime_error(error_.toStdString());
        // Qt pumps stdout/stderr during this wait as well. Wake on the write
        // itself: delivery muxers have no stdout and would otherwise incur a
        // full polling timeout for every compressed video/audio packet.
        process_.waitForBytesWritten(25);
    }
}
void PipeProcess::closeInput() {
    process_.closeWriteChannel();
}
void PipeProcess::finish(JobControl& control) {
    while (process_.state() != QProcess::NotRunning) {
        control.checkpoint();
        drainErrors();
        process_.waitForFinished(25);
    }
    drainErrors();
    if (process_.exitCode() != 0 || process_.exitStatus() != QProcess::NormalExit)
        throw std::runtime_error(error_.toStdString());
}
QString PipeProcess::error() {
    drainErrors();
    return QString::fromUtf8(error_);
}
} // namespace ve
