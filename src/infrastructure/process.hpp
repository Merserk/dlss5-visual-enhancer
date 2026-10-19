#pragma once
#include "job.hpp"
#include <QByteArray>
#include <QProcess>
#include <QStringList>
#include <memory>
#include <functional>

namespace ve {
struct ProcessResult {
    int exitCode = -1;
    QByteArray output;
    QByteArray error;
};
[[nodiscard]] ProcessResult runProcess(const QString& program, const QStringList& arguments,
                                       JobControl* control = nullptr, int timeoutMs = 30000,
                                       const QByteArray& input = {}, const std::function<void(const QByteArray&)>& outputReady = {});
class PipeProcess {
  public:
    // Read-only producers must stop when the consumer stops. Windows QProcess
    // otherwise drains stdout into an unlimited asynchronous memory buffer.
    // Bidirectional filters retain buffered output so writes cannot deadlock.
    enum class OutputMode { Buffered, Backpressured };
    PipeProcess(const QString& program, const QStringList& arguments,
                OutputMode outputMode = OutputMode::Buffered);
    ~PipeProcess();
    PipeProcess(const PipeProcess&) = delete;
    PipeProcess& operator=(const PipeProcess&) = delete;
    [[nodiscard]] QByteArray read(qsizetype size, JobControl& control);
    [[nodiscard]] QByteArray readSome(qsizetype maximum, JobControl& control);
    [[nodiscard]] QByteArray readAvailable(qsizetype maximum, JobControl& control);
    void write(const char* data, qsizetype size, JobControl& control);
    void closeInput();
    void finish(JobControl& control);
    [[nodiscard]] QString error();

  private:
    struct OutputPipe;
    std::unique_ptr<OutputPipe> outputPipe_;
    QProcess process_;
    QByteArray error_;
    QByteArray readOutput(qsizetype maximum);
    void drainErrors();
};
} // namespace ve
