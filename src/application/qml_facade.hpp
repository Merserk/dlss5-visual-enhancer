#pragma once
#include <QObject>
#include <QString>
#include <QVariant>
#include <QVariantList>
#include <QVariantMap>
namespace ve {
enum class Property {
    processingCacheBytes,
    processingCacheReclaimableBytes,
    processingCacheBusy,
    processingCacheCleaning,
    processingCacheReady,
    canCleanProcessingCache,
    processingCacheSizeText,
    processingCacheStatus,
    processingCacheDetails,
    activeTab,
    nrMode,
    workflowStages,
    vfxSettings,
    rrSettings,
    reshadeSettings,
    reshadeStatus,
    reshadePresetInfo,
    vfxStatus,
    sharpeningMethodChoices,
    ffmpegDeviceChoices,
    ffmpegDevice,
    ffmpegDeviceStatus,
    sharpeningMethod,
    casSharpness,
    grainAmount,
    grainSize,
    grainColor,
    grainResponse,
    grainSeed,
    grainAnimated,
    lutFile,
    lutResolutionChoices,
    lutResolution,
    lutAdjustmentValues,
    lutAutoBusy,
    lutAutoMode,
    lutReferenceImage,
    lutReferenceBusy,
    lutReferenceStatus,
    lutSaveBusy,
    lutSaveStatus,
    upscaleMode,
    upscalePreviewLength,
    nrPreviewLength,
    isProcessing,
    overallProgress,
    canPauseExport,
    exportPaused,
    exportPauseRequested,
    exportPhaseText,
    statusMessage,
    previewError,
    splitPosition,
    hasOutputPreview,
    previewInputUrl,
    previewOutputUrl,
    previewPosterUrl,
    previewSourceSeconds,
    previewSourceFrame,
    previewRenderedRanges,
    inputInfoText,
    outputInfoText,
    presetStatus,
    previewBusy,
    operationState,
    fastColorPreviewActive,
    canModifyQueue,
    canPreview,
    canRender,
    canStop,
    runtimeState,
    runtimeError,
    startupWarnings,
    sourceWidth,
    sourceHeight,
    sourceFps,
    sourceDuration,
    outputWidth,
    outputHeight,
    inspectorWidth,
    queueHeight,
    logHeight,
    focusPreview,
    exportDestinationMode,
    exportFolder,
    autoPreviewEnabled,
    languageChoices,
    language,
    gpuName,
    gpuVram,
    gpuCount,
    nrImageQueue,
    nrVideoQueue,
    upscaleImageQueue,
    upscaleVideoQueue,
    fiQueue,
    previewInputIsVideo,
    previewOutputIsVideo,
    windowWidth,
    windowHeight,
    windowX,
    windowY,
    windowMaximized,
    appVersion,
    diagnosticsText,
    nrStyleChoices,
    nrScaleChoices,
    imageScalingFilterChoices,
    videoScalingFilterChoices,
    dlssModeChoices,
    dlssPresetChoices,
    upscaleEngineChoices,
    imageFormatChoices,
    containerChoices,
    upscaleContainerChoices,
    fiContainerChoices,
    codecChoices,
    fixedQualityCodecs,
    encodingQualityChoices,
    renameModeChoices,
    fiFpsChoices,
    fiProviderChoices,
    fsrAdapterChoices,
    fiPreviewLengthChoices,
    upscalePreviewLengthChoices,
    nrPreviewLengthChoices,
    imageSizeModeChoices,
    videoSizeModeChoices,
    imageScaleFactorChoices,
    videoScaleFactorChoices,
    vsrQualityChoices,
    hdrPrecisionChoices,
    aiGpuChoices,
    videoGpuChoices,
    nrStyle,
    upscalingFactor,
    imageScalingFilter,
    videoScalingFilter,
    nrScaleMethod,
    nrDlssMode,
    nrDlssPreset,
    nrIntensity,
    nrPasses,
    opticalFlowQualityChoices,
    opticalFlowQuality,
    localToneStrength,
    localStructureStrength,
    skinStructureStrength,
    automaticMask,
    customMaskStatus,
    imageFormat,
    imageQuality,
    imageBitDepth,
    imageRenameMode,
    imageCustomSuffix,
    videoCodec,
    videoContainer,
    videoQuality,
    videoHdrMode,
    videoRenameMode,
    videoCustomSuffix,
    upscaleImageEngine,
    upscaleImageDlssMode,
    upscaleImageDlssPreset,
    upscaleImageVsrQuality,
    upscaleImageSizeMode,
    upscaleImageScaleFactor,
    upscaleImageWidth,
    upscaleImageHeight,
    upscaleImageAspectLock,
    upscaleImageOutputFormat,
    upscaleImageQuality,
    upscaleEngine,
    upscaleDlssMode,
    upscaleDlssPreset,
    upscaleOpticalFlowQuality,
    upscaleVsrEnabled,
    upscaleVsrQuality,
    upscaleSizeMode,
    upscaleScaleFactor,
    upscaleWidth,
    upscaleHeight,
    upscaleAspectLock,
    upscaleHdrEnabled,
    upscaleHdrContrast,
    upscaleHdrSaturation,
    upscaleHdrMiddleGray,
    upscaleHdrPeakLuminance,
    upscaleHdrPrecision,
    upscaleCodec,
    upscaleContainer,
    upscaleQuality,
    upscaleImageRenameMode,
    upscaleImageCustomSuffix,
    upscaleRenameMode,
    upscaleCustomSuffix,
    fiFrameRateMode,
    fiMultiplier,
    fiTargetFps,
    fiProvider,
    fsrFrameRateMode,
    fsrMultiplier,
    fsrTargetFps,
    fsrAdapterId,
    fsrStatus,
    fiOpticalFlowQuality,
    fiCodec,
    fiContainer,
    fiQuality,
    fiPreviewLength,
    fiHdrMode,
    fiRenameMode,
    fiCustomSuffix,
    videoHdrSupported,
    fiHdrSupported,
    upscaleHdrSupported,
    aiGpuUuid,
    videoGpuUuid,
    renderValidationMessage,
    frameGenerationEstimate,
    rtxSuperResolutionEstimate,
    outputEstimate,
    currentLogPath,
    fullLog
};
enum class Action {
    refreshProcessingCache,
    cleanProcessingCache,
    setVfxSetting,
    setRrSetting,
    setReShadeSetting,
    reloadReShadePreset,
    saveReShadePresetCopy,
    setReShadeParameter,
    resetReShadeParameters,
    setReShadeTechniqueEnabled,
    moveReShadeTechnique,
    setLutFile,
    setLutAdjustment,
    resetLutAdjustments,
    autoAdjustLut,
    autoAdjustLutFixedFrames,
    setLutReferenceImage,
    saveLut,
    setWorkflowStageEnabled,
    moveWorkflowStage,
    saveWindowLayout,
    initializeRuntime,
    selectCustomMask,
    clearCustomMask,
    resetToDefaults,
    resetTabSettings,
    exportPreset,
    exportPresetTo,
    importPreset,
    addFiles,
    _sweep_temp_caches,
    selectQueueItem,
    removeQueueItem,
    clearActiveQueue,
    clearSelectedPreviewItem,
    showSelectedInExplorer,
    showInExplorer,
    clearCompletedQueueItems,
    retryFailedQueueItems,
    startActiveBatch,
    requestActiveBatchExport,
    startActiveBatchWithExport,
    pauseExport,
    toggleExportPause,
    stopActiveBatch,
    renderPreview,
    notePlayheadMs,
    renderPreviewAt,
    schedulePreviewAt,
    refreshVideoPoster,
    shutdown,
    openFolder,
    openThirdPartyNotices,
    copyToClipboard,
    pasteFromClipboard,
    _append_backend_log,
    clearLogDisplay
};
// Stable facade for the existing QML. Rendering logic lives in feature services.
class QmlFacade : public QObject {
    Q_OBJECT
    Q_PROPERTY(qint64 processingCacheBytes READ processingCacheBytes NOTIFY processingCacheChanged)
    Q_PROPERTY(qint64 processingCacheReclaimableBytes READ processingCacheReclaimableBytes NOTIFY processingCacheChanged)
    Q_PROPERTY(bool processingCacheBusy READ processingCacheBusy NOTIFY processingCacheChanged)
    Q_PROPERTY(bool processingCacheCleaning READ processingCacheCleaning NOTIFY processingCacheChanged)
    Q_PROPERTY(bool processingCacheReady READ processingCacheReady NOTIFY processingCacheChanged)
    Q_PROPERTY(bool canCleanProcessingCache READ canCleanProcessingCache NOTIFY processingCacheChanged)
    Q_PROPERTY(QString processingCacheSizeText READ processingCacheSizeText NOTIFY processingCacheChanged)
    Q_PROPERTY(QString processingCacheStatus READ processingCacheStatus NOTIFY processingCacheChanged)
    Q_PROPERTY(QString processingCacheDetails READ processingCacheDetails NOTIFY processingCacheChanged)
    Q_PROPERTY(QVariantMap vfxSettings READ vfxSettings NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantMap rrSettings READ rrSettings NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantMap reshadeSettings READ reshadeSettings NOTIFY settingsUpdated)
    Q_PROPERTY(QString reshadeStatus READ reshadeStatus NOTIFY reshadeStatusChanged)
    Q_PROPERTY(QVariantMap reshadePresetInfo READ reshadePresetInfo NOTIFY reshadePresetInfoChanged)
    Q_PROPERTY(QString vfxStatus READ vfxStatus NOTIFY vfxStatusChanged)
    Q_PROPERTY(QString activeTab READ activeTab WRITE write_activeTab NOTIFY activeTabChanged)
    Q_PROPERTY(QString nrMode READ nrMode WRITE write_nrMode NOTIFY nrModeChanged)
    Q_PROPERTY(QVariantList workflowStages READ workflowStages NOTIFY workflowStagesChanged)
    Q_PROPERTY(QVariantList sharpeningMethodChoices READ sharpeningMethodChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList ffmpegDeviceChoices READ ffmpegDeviceChoices NOTIFY gpuChoicesChanged)
    Q_PROPERTY(QString ffmpegDevice READ ffmpegDevice WRITE write_ffmpegDevice NOTIFY settingsUpdated)
    Q_PROPERTY(QString ffmpegDeviceStatus READ ffmpegDeviceStatus NOTIFY settingsUpdated)
    Q_PROPERTY(
        QString sharpeningMethod READ sharpeningMethod WRITE write_sharpeningMethod NOTIFY settingsUpdated)
    Q_PROPERTY(int casSharpness READ casSharpness WRITE write_casSharpness NOTIFY settingsUpdated)
    Q_PROPERTY(int grainAmount READ grainAmount WRITE write_grainAmount NOTIFY settingsUpdated)
    Q_PROPERTY(double grainSize READ grainSize WRITE write_grainSize NOTIFY settingsUpdated)
    Q_PROPERTY(int grainColor READ grainColor WRITE write_grainColor NOTIFY settingsUpdated)
    Q_PROPERTY(int grainResponse READ grainResponse WRITE write_grainResponse NOTIFY settingsUpdated)
    Q_PROPERTY(int grainSeed READ grainSeed WRITE write_grainSeed NOTIFY settingsUpdated)
    Q_PROPERTY(bool grainAnimated READ grainAnimated WRITE write_grainAnimated NOTIFY settingsUpdated)
    Q_PROPERTY(QString lutFile READ lutFile NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList lutResolutionChoices READ lutResolutionChoices NOTIFY settingsUpdated)
    Q_PROPERTY(int lutResolution READ lutResolution WRITE write_lutResolution NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantMap lutAdjustmentValues READ lutAdjustmentValues NOTIFY settingsUpdated)
    Q_PROPERTY(bool lutAutoBusy READ lutAutoBusy NOTIFY lutAutoChanged)
    Q_PROPERTY(QString lutAutoMode READ lutAutoMode NOTIFY lutAutoChanged)
    Q_PROPERTY(QString lutReferenceImage READ lutReferenceImage NOTIFY settingsUpdated)
    Q_PROPERTY(bool lutReferenceBusy READ lutReferenceBusy NOTIFY lutReferenceChanged)
    Q_PROPERTY(QString lutReferenceStatus READ lutReferenceStatus NOTIFY lutReferenceChanged)
    Q_PROPERTY(bool lutSaveBusy READ lutSaveBusy NOTIFY lutSaveChanged)
    Q_PROPERTY(QString lutSaveStatus READ lutSaveStatus NOTIFY lutSaveChanged)
    Q_PROPERTY(QString upscaleMode READ upscaleMode WRITE write_upscaleMode NOTIFY upscaleModeChanged)
    Q_PROPERTY(QString upscalePreviewLength READ upscalePreviewLength WRITE write_upscalePreviewLength NOTIFY
                   settingsUpdated)
    Q_PROPERTY(
        QString nrPreviewLength READ nrPreviewLength WRITE write_nrPreviewLength NOTIFY settingsUpdated)
    Q_PROPERTY(bool isProcessing READ isProcessing NOTIFY isProcessingChanged)
    Q_PROPERTY(double overallProgress READ overallProgress NOTIFY overallProgressChanged)
    Q_PROPERTY(bool canPauseExport READ canPauseExport NOTIFY operationStateChanged)
    Q_PROPERTY(bool exportPaused READ exportPaused NOTIFY exportPhaseChanged)
    Q_PROPERTY(bool exportPauseRequested READ exportPauseRequested NOTIFY exportPhaseChanged)
    Q_PROPERTY(QString exportPhaseText READ exportPhaseText NOTIFY exportPhaseChanged)
    Q_PROPERTY(QString statusMessage READ statusMessage NOTIFY statusMessageChanged)
    Q_PROPERTY(QString previewError READ previewError NOTIFY mediaPreviewChanged)
    Q_PROPERTY(double splitPosition READ splitPosition WRITE write_splitPosition NOTIFY splitPositionChanged)
    Q_PROPERTY(bool hasOutputPreview READ hasOutputPreview NOTIFY hasOutputPreviewChanged)
    Q_PROPERTY(QString previewInputUrl READ previewInputUrl NOTIFY previewInputUrlChanged)
    Q_PROPERTY(QString previewOutputUrl READ previewOutputUrl NOTIFY previewOutputUrlChanged)
    Q_PROPERTY(QString previewPosterUrl READ previewPosterUrl NOTIFY previewPosterUrlChanged)
    Q_PROPERTY(double previewSourceSeconds READ previewSourceSeconds NOTIFY previewSourceChanged)
    Q_PROPERTY(int previewSourceFrame READ previewSourceFrame NOTIFY previewSourceChanged)
    Q_PROPERTY(QVariantList previewRenderedRanges READ previewRenderedRanges NOTIFY previewRangesChanged)
    Q_PROPERTY(QString inputInfoText READ inputInfoText NOTIFY inputInfoTextChanged)
    Q_PROPERTY(QString outputInfoText READ outputInfoText NOTIFY outputInfoTextChanged)
    Q_PROPERTY(QString presetStatus READ presetStatus NOTIFY presetStatusChanged)
    Q_PROPERTY(bool previewBusy READ previewBusy NOTIFY previewBusyChanged)
    Q_PROPERTY(QString operationState READ operationState NOTIFY operationStateChanged)
    Q_PROPERTY(bool fastColorPreviewActive READ fastColorPreviewActive NOTIFY operationStateChanged)
    Q_PROPERTY(bool canModifyQueue READ canModifyQueue NOTIFY operationStateChanged)
    Q_PROPERTY(bool canPreview READ canPreview NOTIFY operationStateChanged)
    Q_PROPERTY(bool canRender READ canRender NOTIFY operationStateChanged)
    Q_PROPERTY(bool canStop READ canStop NOTIFY operationStateChanged)
    Q_PROPERTY(QString runtimeState READ runtimeState NOTIFY runtimeStateChanged)
    Q_PROPERTY(QString runtimeError READ runtimeError NOTIFY runtimeStateChanged)
    Q_PROPERTY(QString startupWarnings READ startupWarnings NOTIFY runtimeStateChanged)
    Q_PROPERTY(int sourceWidth READ sourceWidth NOTIFY sourceSizeChanged)
    Q_PROPERTY(int sourceHeight READ sourceHeight NOTIFY sourceSizeChanged)
    Q_PROPERTY(double sourceFps READ sourceFps NOTIFY sourceSizeChanged)
    Q_PROPERTY(double sourceDuration READ sourceDuration NOTIFY sourceSizeChanged)
    Q_PROPERTY(int outputWidth READ outputWidth NOTIFY outputSizeChanged)
    Q_PROPERTY(int outputHeight READ outputHeight NOTIFY outputSizeChanged)
    Q_PROPERTY(int inspectorWidth READ inspectorWidth WRITE write_inspectorWidth NOTIFY layoutChanged)
    Q_PROPERTY(int queueHeight READ queueHeight WRITE write_queueHeight NOTIFY layoutChanged)
    Q_PROPERTY(int logHeight READ logHeight WRITE write_logHeight NOTIFY layoutChanged)
    Q_PROPERTY(bool focusPreview READ focusPreview WRITE write_focusPreview NOTIFY layoutChanged)
    Q_PROPERTY(QString exportDestinationMode READ exportDestinationMode WRITE write_exportDestinationMode
                   NOTIFY exportPrefsChanged)
    Q_PROPERTY(QString exportFolder READ exportFolder WRITE write_exportFolder NOTIFY exportPrefsChanged)
    Q_PROPERTY(bool autoPreviewEnabled READ autoPreviewEnabled WRITE write_autoPreviewEnabled NOTIFY
                   autoPreviewChanged)
    Q_PROPERTY(QVariantList languageChoices READ languageChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QString language READ language WRITE write_language NOTIFY languageChanged)
    Q_PROPERTY(QString gpuName READ gpuName NOTIFY runtimeStateChanged)
    Q_PROPERTY(QString gpuVram READ gpuVram NOTIFY runtimeStateChanged)
    Q_PROPERTY(int gpuCount READ gpuCount NOTIFY runtimeStateChanged)
    Q_PROPERTY(QObject* nrImageQueue READ nrImageQueue NOTIFY settingsUpdated)
    Q_PROPERTY(QObject* nrVideoQueue READ nrVideoQueue NOTIFY settingsUpdated)
    Q_PROPERTY(QObject* upscaleImageQueue READ upscaleImageQueue NOTIFY settingsUpdated)
    Q_PROPERTY(QObject* upscaleVideoQueue READ upscaleVideoQueue NOTIFY settingsUpdated)
    Q_PROPERTY(QObject* fiQueue READ fiQueue NOTIFY settingsUpdated)
    Q_PROPERTY(bool previewInputIsVideo READ previewInputIsVideo NOTIFY mediaPreviewChanged)
    Q_PROPERTY(bool previewOutputIsVideo READ previewOutputIsVideo NOTIFY mediaPreviewChanged)
    Q_PROPERTY(int windowWidth READ windowWidth NOTIFY layoutChanged)
    Q_PROPERTY(int windowHeight READ windowHeight NOTIFY layoutChanged)
    Q_PROPERTY(int windowX READ windowX NOTIFY layoutChanged)
    Q_PROPERTY(int windowY READ windowY NOTIFY layoutChanged)
    Q_PROPERTY(bool windowMaximized READ windowMaximized NOTIFY layoutChanged)
    Q_PROPERTY(QString appVersion READ appVersion NOTIFY settingsUpdated)
    Q_PROPERTY(QString diagnosticsText READ diagnosticsText NOTIFY runtimeStateChanged)
    Q_PROPERTY(QVariantList nrStyleChoices READ nrStyleChoices NOTIFY languageChanged)
    Q_PROPERTY(QVariantList nrScaleChoices READ nrScaleChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList imageScalingFilterChoices READ imageScalingFilterChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList videoScalingFilterChoices READ videoScalingFilterChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList dlssModeChoices READ dlssModeChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList dlssPresetChoices READ dlssPresetChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList upscaleEngineChoices READ upscaleEngineChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList imageFormatChoices READ imageFormatChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList containerChoices READ containerChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList upscaleContainerChoices READ upscaleContainerChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList fiContainerChoices READ fiContainerChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList codecChoices READ codecChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList fixedQualityCodecs READ fixedQualityCodecs NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList encodingQualityChoices READ encodingQualityChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList renameModeChoices READ renameModeChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList fiFpsChoices READ fiFpsChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList fiProviderChoices READ fiProviderChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList fsrAdapterChoices READ fsrAdapterChoices NOTIFY gpuChoicesChanged)
    Q_PROPERTY(QVariantList fiPreviewLengthChoices READ fiPreviewLengthChoices NOTIFY languageChanged)
    Q_PROPERTY(
        QVariantList upscalePreviewLengthChoices READ upscalePreviewLengthChoices NOTIFY languageChanged)
    Q_PROPERTY(QVariantList nrPreviewLengthChoices READ nrPreviewLengthChoices NOTIFY languageChanged)
    Q_PROPERTY(QVariantList imageSizeModeChoices READ imageSizeModeChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList videoSizeModeChoices READ videoSizeModeChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList imageScaleFactorChoices READ imageScaleFactorChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList videoScaleFactorChoices READ videoScaleFactorChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList vsrQualityChoices READ vsrQualityChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList hdrPrecisionChoices READ hdrPrecisionChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList aiGpuChoices READ aiGpuChoices NOTIFY gpuChoicesChanged)
    Q_PROPERTY(QVariantList videoGpuChoices READ videoGpuChoices NOTIFY gpuChoicesChanged)
    Q_PROPERTY(QString nrStyle READ nrStyle WRITE write_nrStyle NOTIFY settingsUpdated)
    Q_PROPERTY(double upscalingFactor READ upscalingFactor WRITE write_upscalingFactor NOTIFY settingsUpdated)
    Q_PROPERTY(QString imageScalingFilter READ imageScalingFilter WRITE write_imageScalingFilter NOTIFY
                   settingsUpdated)
    Q_PROPERTY(QString videoScalingFilter READ videoScalingFilter WRITE write_videoScalingFilter NOTIFY
                   settingsUpdated)
    Q_PROPERTY(QString nrScaleMethod READ nrScaleMethod WRITE write_nrScaleMethod NOTIFY settingsUpdated)
    Q_PROPERTY(QString nrDlssMode READ nrDlssMode WRITE write_nrDlssMode NOTIFY settingsUpdated)
    Q_PROPERTY(QString nrDlssPreset READ nrDlssPreset WRITE write_nrDlssPreset NOTIFY settingsUpdated)
    Q_PROPERTY(double nrIntensity READ nrIntensity WRITE write_nrIntensity NOTIFY settingsUpdated)
    Q_PROPERTY(int nrPasses READ nrPasses WRITE write_nrPasses NOTIFY settingsUpdated)
    Q_PROPERTY(QVariantList opticalFlowQualityChoices READ opticalFlowQualityChoices NOTIFY settingsUpdated)
    Q_PROPERTY(QString opticalFlowQuality READ opticalFlowQuality WRITE write_opticalFlowQuality NOTIFY
                   settingsUpdated)
    Q_PROPERTY(
        double localToneStrength READ localToneStrength WRITE write_localToneStrength NOTIFY settingsUpdated)
    Q_PROPERTY(double localStructureStrength READ localStructureStrength WRITE write_localStructureStrength
                   NOTIFY settingsUpdated)
    Q_PROPERTY(double skinStructureStrength READ skinStructureStrength WRITE write_skinStructureStrength
                   NOTIFY settingsUpdated)
    Q_PROPERTY(bool automaticMask READ automaticMask WRITE write_automaticMask NOTIFY settingsUpdated)
    Q_PROPERTY(QString customMaskStatus READ customMaskStatus NOTIFY settingsUpdated)
    Q_PROPERTY(QString imageFormat READ imageFormat WRITE write_imageFormat NOTIFY settingsUpdated)
    Q_PROPERTY(int imageQuality READ imageQuality WRITE write_imageQuality NOTIFY settingsUpdated)
    Q_PROPERTY(int imageBitDepth READ imageBitDepth WRITE write_imageBitDepth NOTIFY settingsUpdated)
    Q_PROPERTY(
        QString imageRenameMode READ imageRenameMode WRITE write_imageRenameMode NOTIFY settingsUpdated)
    Q_PROPERTY(
        QString imageCustomSuffix READ imageCustomSuffix WRITE write_imageCustomSuffix NOTIFY settingsUpdated)
    Q_PROPERTY(QString videoCodec READ videoCodec WRITE write_videoCodec NOTIFY settingsUpdated)
    Q_PROPERTY(QString videoContainer READ videoContainer WRITE write_videoContainer NOTIFY settingsUpdated)
    Q_PROPERTY(QString videoQuality READ videoQuality WRITE write_videoQuality NOTIFY settingsUpdated)
    Q_PROPERTY(bool videoHdrMode READ videoHdrMode WRITE write_videoHdrMode NOTIFY settingsUpdated)
    Q_PROPERTY(
        QString videoRenameMode READ videoRenameMode WRITE write_videoRenameMode NOTIFY settingsUpdated)
    Q_PROPERTY(
        QString videoCustomSuffix READ videoCustomSuffix WRITE write_videoCustomSuffix NOTIFY settingsUpdated)
    Q_PROPERTY(QString upscaleImageEngine READ upscaleImageEngine WRITE write_upscaleImageEngine NOTIFY
                   settingsUpdated)
    Q_PROPERTY(QString upscaleImageDlssMode READ upscaleImageDlssMode WRITE write_upscaleImageDlssMode NOTIFY
                   settingsUpdated)
    Q_PROPERTY(QString upscaleImageDlssPreset READ upscaleImageDlssPreset WRITE write_upscaleImageDlssPreset
                   NOTIFY settingsUpdated)
    Q_PROPERTY(int upscaleImageVsrQuality READ upscaleImageVsrQuality WRITE write_upscaleImageVsrQuality
                   NOTIFY settingsUpdated)
    Q_PROPERTY(QString upscaleImageSizeMode READ upscaleImageSizeMode WRITE write_upscaleImageSizeMode NOTIFY
                   settingsUpdated)
    Q_PROPERTY(double upscaleImageScaleFactor READ upscaleImageScaleFactor WRITE write_upscaleImageScaleFactor
                   NOTIFY settingsUpdated)
    Q_PROPERTY(
        int upscaleImageWidth READ upscaleImageWidth WRITE write_upscaleImageWidth NOTIFY settingsUpdated)
    Q_PROPERTY(
        int upscaleImageHeight READ upscaleImageHeight WRITE write_upscaleImageHeight NOTIFY settingsUpdated)
    Q_PROPERTY(bool upscaleImageAspectLock READ upscaleImageAspectLock WRITE write_upscaleImageAspectLock
                   NOTIFY settingsUpdated)
    Q_PROPERTY(QString upscaleImageOutputFormat READ upscaleImageOutputFormat WRITE
                   write_upscaleImageOutputFormat NOTIFY settingsUpdated)
    Q_PROPERTY(int upscaleImageQuality READ upscaleImageQuality WRITE write_upscaleImageQuality NOTIFY
                   settingsUpdated)
    Q_PROPERTY(QString upscaleEngine READ upscaleEngine WRITE write_upscaleEngine NOTIFY settingsUpdated)
    Q_PROPERTY(
        QString upscaleDlssMode READ upscaleDlssMode WRITE write_upscaleDlssMode NOTIFY settingsUpdated)
    Q_PROPERTY(
        QString upscaleDlssPreset READ upscaleDlssPreset WRITE write_upscaleDlssPreset NOTIFY settingsUpdated)
    Q_PROPERTY(QString upscaleOpticalFlowQuality READ upscaleOpticalFlowQuality WRITE
                   write_upscaleOpticalFlowQuality NOTIFY settingsUpdated)
    Q_PROPERTY(
        bool upscaleVsrEnabled READ upscaleVsrEnabled WRITE write_upscaleVsrEnabled NOTIFY settingsUpdated)
    Q_PROPERTY(
        int upscaleVsrQuality READ upscaleVsrQuality WRITE write_upscaleVsrQuality NOTIFY settingsUpdated)
    Q_PROPERTY(
        QString upscaleSizeMode READ upscaleSizeMode WRITE write_upscaleSizeMode NOTIFY settingsUpdated)
    Q_PROPERTY(double upscaleScaleFactor READ upscaleScaleFactor WRITE write_upscaleScaleFactor NOTIFY
                   settingsUpdated)
    Q_PROPERTY(int upscaleWidth READ upscaleWidth WRITE write_upscaleWidth NOTIFY settingsUpdated)
    Q_PROPERTY(int upscaleHeight READ upscaleHeight WRITE write_upscaleHeight NOTIFY settingsUpdated)
    Q_PROPERTY(
        bool upscaleAspectLock READ upscaleAspectLock WRITE write_upscaleAspectLock NOTIFY settingsUpdated)
    Q_PROPERTY(
        bool upscaleHdrEnabled READ upscaleHdrEnabled WRITE write_upscaleHdrEnabled NOTIFY settingsUpdated)
    Q_PROPERTY(
        int upscaleHdrContrast READ upscaleHdrContrast WRITE write_upscaleHdrContrast NOTIFY settingsUpdated)
    Q_PROPERTY(int upscaleHdrSaturation READ upscaleHdrSaturation WRITE write_upscaleHdrSaturation NOTIFY
                   settingsUpdated)
    Q_PROPERTY(int upscaleHdrMiddleGray READ upscaleHdrMiddleGray WRITE write_upscaleHdrMiddleGray NOTIFY
                   settingsUpdated)
    Q_PROPERTY(int upscaleHdrPeakLuminance READ upscaleHdrPeakLuminance WRITE write_upscaleHdrPeakLuminance
                   NOTIFY settingsUpdated)
    Q_PROPERTY(QString upscaleHdrPrecision READ upscaleHdrPrecision WRITE write_upscaleHdrPrecision NOTIFY
                   settingsUpdated)
    Q_PROPERTY(QString upscaleCodec READ upscaleCodec WRITE write_upscaleCodec NOTIFY settingsUpdated)
    Q_PROPERTY(
        QString upscaleContainer READ upscaleContainer WRITE write_upscaleContainer NOTIFY settingsUpdated)
    Q_PROPERTY(QString upscaleQuality READ upscaleQuality WRITE write_upscaleQuality NOTIFY settingsUpdated)
    Q_PROPERTY(QString upscaleImageRenameMode READ upscaleImageRenameMode WRITE write_upscaleImageRenameMode
                   NOTIFY settingsUpdated)
    Q_PROPERTY(QString upscaleImageCustomSuffix READ upscaleImageCustomSuffix WRITE
                   write_upscaleImageCustomSuffix NOTIFY settingsUpdated)
    Q_PROPERTY(
        QString upscaleRenameMode READ upscaleRenameMode WRITE write_upscaleRenameMode NOTIFY settingsUpdated)
    Q_PROPERTY(QString upscaleCustomSuffix READ upscaleCustomSuffix WRITE write_upscaleCustomSuffix NOTIFY
                   settingsUpdated)
    Q_PROPERTY(QString fiFrameRateMode READ fiFrameRateMode WRITE write_fiFrameRateMode NOTIFY settingsUpdated)
    Q_PROPERTY(int fiMultiplier READ fiMultiplier WRITE write_fiMultiplier NOTIFY settingsUpdated)
    Q_PROPERTY(QString fiTargetFps READ fiTargetFps WRITE write_fiTargetFps NOTIFY settingsUpdated)
    Q_PROPERTY(QString fiProvider READ fiProvider WRITE write_fiProvider NOTIFY settingsUpdated)
    Q_PROPERTY(QString fsrFrameRateMode READ fsrFrameRateMode WRITE write_fsrFrameRateMode NOTIFY settingsUpdated)
    Q_PROPERTY(int fsrMultiplier READ fsrMultiplier WRITE write_fsrMultiplier NOTIFY settingsUpdated)
    Q_PROPERTY(QString fsrTargetFps READ fsrTargetFps WRITE write_fsrTargetFps NOTIFY settingsUpdated)
    Q_PROPERTY(QString fsrAdapterId READ fsrAdapterId WRITE write_fsrAdapterId NOTIFY settingsUpdated)
    Q_PROPERTY(QString fsrStatus READ fsrStatus NOTIFY runtimeStateChanged)
    Q_PROPERTY(QString fiOpticalFlowQuality READ fiOpticalFlowQuality WRITE write_fiOpticalFlowQuality NOTIFY
                   settingsUpdated)
    Q_PROPERTY(QString fiCodec READ fiCodec WRITE write_fiCodec NOTIFY settingsUpdated)
    Q_PROPERTY(QString fiContainer READ fiContainer WRITE write_fiContainer NOTIFY settingsUpdated)
    Q_PROPERTY(QString fiQuality READ fiQuality WRITE write_fiQuality NOTIFY settingsUpdated)
    Q_PROPERTY(
        QString fiPreviewLength READ fiPreviewLength WRITE write_fiPreviewLength NOTIFY settingsUpdated)
    Q_PROPERTY(bool fiHdrMode READ fiHdrMode WRITE write_fiHdrMode NOTIFY settingsUpdated)
    Q_PROPERTY(QString fiRenameMode READ fiRenameMode WRITE write_fiRenameMode NOTIFY settingsUpdated)
    Q_PROPERTY(QString fiCustomSuffix READ fiCustomSuffix WRITE write_fiCustomSuffix NOTIFY settingsUpdated)
    Q_PROPERTY(bool videoHdrSupported READ videoHdrSupported NOTIFY settingsUpdated)
    Q_PROPERTY(bool fiHdrSupported READ fiHdrSupported NOTIFY settingsUpdated)
    Q_PROPERTY(bool upscaleHdrSupported READ upscaleHdrSupported NOTIFY settingsUpdated)
    Q_PROPERTY(QString aiGpuUuid READ aiGpuUuid WRITE write_aiGpuUuid NOTIFY settingsUpdated)
    Q_PROPERTY(QString videoGpuUuid READ videoGpuUuid WRITE write_videoGpuUuid NOTIFY settingsUpdated)
    Q_PROPERTY(QString renderValidationMessage READ renderValidationMessage NOTIFY estimateChanged)
    Q_PROPERTY(QString frameGenerationEstimate READ frameGenerationEstimate NOTIFY estimateChanged)
    Q_PROPERTY(QString rtxSuperResolutionEstimate READ rtxSuperResolutionEstimate NOTIFY estimateChanged)
    Q_PROPERTY(QString outputEstimate READ outputEstimate NOTIFY estimateChanged)
    Q_PROPERTY(QString currentLogPath READ currentLogPath NOTIFY logAppended)
    Q_PROPERTY(QString fullLog READ fullLog NOTIFY logAppended)
  public:
    using QObject::QObject;
    QString activeTab() const { return read(Property::activeTab).toString(); }
    void write_activeTab(QString value) { write(Property::activeTab, QVariant::fromValue(value)); }
    QString nrMode() const { return read(Property::nrMode).toString(); }
    void write_nrMode(QString value) { write(Property::nrMode, QVariant::fromValue(value)); }
    QVariantList workflowStages() const { return read(Property::workflowStages).toList(); }
    QVariantList sharpeningMethodChoices() const { return read(Property::sharpeningMethodChoices).toList(); }
    QVariantList ffmpegDeviceChoices() const { return read(Property::ffmpegDeviceChoices).toList(); }
    QString ffmpegDevice() const { return read(Property::ffmpegDevice).toString(); }
    void write_ffmpegDevice(QString value) { write(Property::ffmpegDevice, QVariant::fromValue(value)); }
    QString ffmpegDeviceStatus() const { return read(Property::ffmpegDeviceStatus).toString(); }
    QString sharpeningMethod() const { return read(Property::sharpeningMethod).toString(); }
    void write_sharpeningMethod(QString value) {
        write(Property::sharpeningMethod, QVariant::fromValue(value));
    }
    int casSharpness() const { return read(Property::casSharpness).toInt(); }
    void write_casSharpness(int value) { write(Property::casSharpness, QVariant::fromValue(value)); }
    int grainAmount() const { return read(Property::grainAmount).toInt(); }
    void write_grainAmount(int value) { write(Property::grainAmount, QVariant::fromValue(value)); }
    double grainSize() const { return read(Property::grainSize).toDouble(); }
    void write_grainSize(double value) { write(Property::grainSize, QVariant::fromValue(value)); }
    int grainColor() const { return read(Property::grainColor).toInt(); }
    void write_grainColor(int value) { write(Property::grainColor, QVariant::fromValue(value)); }
    int grainResponse() const { return read(Property::grainResponse).toInt(); }
    void write_grainResponse(int value) { write(Property::grainResponse, QVariant::fromValue(value)); }
    int grainSeed() const { return read(Property::grainSeed).toInt(); }
    void write_grainSeed(int value) { write(Property::grainSeed, QVariant::fromValue(value)); }
    bool grainAnimated() const { return read(Property::grainAnimated).toBool(); }
    void write_grainAnimated(bool value) { write(Property::grainAnimated, QVariant::fromValue(value)); }
    QString lutFile() const { return read(Property::lutFile).toString(); }
    QVariantList lutResolutionChoices() const { return read(Property::lutResolutionChoices).toList(); }
    int lutResolution() const { return read(Property::lutResolution).toInt(); }
    void write_lutResolution(int value) { write(Property::lutResolution, QVariant::fromValue(value)); }
    QVariantMap lutAdjustmentValues() const { return read(Property::lutAdjustmentValues).toMap(); }
    bool lutAutoBusy() const { return read(Property::lutAutoBusy).toBool(); }
    QString lutAutoMode() const { return read(Property::lutAutoMode).toString(); }
    QString lutReferenceImage() const { return read(Property::lutReferenceImage).toString(); }
    bool lutReferenceBusy() const { return read(Property::lutReferenceBusy).toBool(); }
    QString lutReferenceStatus() const { return read(Property::lutReferenceStatus).toString(); }
    bool lutSaveBusy() const { return read(Property::lutSaveBusy).toBool(); }
    QString lutSaveStatus() const { return read(Property::lutSaveStatus).toString(); }
    QString upscaleMode() const { return read(Property::upscaleMode).toString(); }
    void write_upscaleMode(QString value) { write(Property::upscaleMode, QVariant::fromValue(value)); }
    QString upscalePreviewLength() const { return read(Property::upscalePreviewLength).toString(); }
    void write_upscalePreviewLength(QString value) {
        write(Property::upscalePreviewLength, QVariant::fromValue(value));
    }
    QString nrPreviewLength() const { return read(Property::nrPreviewLength).toString(); }
    void write_nrPreviewLength(QString value) {
        write(Property::nrPreviewLength, QVariant::fromValue(value));
    }
    bool isProcessing() const { return read(Property::isProcessing).toBool(); }
    double overallProgress() const { return read(Property::overallProgress).toDouble(); }
    bool canPauseExport() const { return read(Property::canPauseExport).toBool(); }
    bool exportPaused() const { return read(Property::exportPaused).toBool(); }
    bool exportPauseRequested() const { return read(Property::exportPauseRequested).toBool(); }
    QString exportPhaseText() const { return read(Property::exportPhaseText).toString(); }
    QString statusMessage() const { return read(Property::statusMessage).toString(); }
    QString previewError() const { return read(Property::previewError).toString(); }
    double splitPosition() const { return read(Property::splitPosition).toDouble(); }
    void write_splitPosition(double value) { write(Property::splitPosition, QVariant::fromValue(value)); }
    bool hasOutputPreview() const { return read(Property::hasOutputPreview).toBool(); }
    QString previewInputUrl() const { return read(Property::previewInputUrl).toString(); }
    QString previewOutputUrl() const { return read(Property::previewOutputUrl).toString(); }
    QString previewPosterUrl() const { return read(Property::previewPosterUrl).toString(); }
    double previewSourceSeconds() const { return read(Property::previewSourceSeconds).toDouble(); }
    int previewSourceFrame() const { return read(Property::previewSourceFrame).toInt(); }
    QVariantList previewRenderedRanges() const { return read(Property::previewRenderedRanges).toList(); }
    QString inputInfoText() const { return read(Property::inputInfoText).toString(); }
    QString outputInfoText() const { return read(Property::outputInfoText).toString(); }
    QString presetStatus() const { return read(Property::presetStatus).toString(); }
    bool previewBusy() const { return read(Property::previewBusy).toBool(); }
    QString operationState() const { return read(Property::operationState).toString(); }
    bool fastColorPreviewActive() const { return read(Property::fastColorPreviewActive).toBool(); }
    bool canModifyQueue() const { return read(Property::canModifyQueue).toBool(); }
    bool canPreview() const { return read(Property::canPreview).toBool(); }
    bool canRender() const { return read(Property::canRender).toBool(); }
    bool canStop() const { return read(Property::canStop).toBool(); }
    QString runtimeState() const { return read(Property::runtimeState).toString(); }
    QString runtimeError() const { return read(Property::runtimeError).toString(); }
    QString startupWarnings() const { return read(Property::startupWarnings).toString(); }
    int sourceWidth() const { return read(Property::sourceWidth).toInt(); }
    int sourceHeight() const { return read(Property::sourceHeight).toInt(); }
    double sourceFps() const { return read(Property::sourceFps).toDouble(); }
    double sourceDuration() const { return read(Property::sourceDuration).toDouble(); }
    int outputWidth() const { return read(Property::outputWidth).toInt(); }
    int outputHeight() const { return read(Property::outputHeight).toInt(); }
    int inspectorWidth() const { return read(Property::inspectorWidth).toInt(); }
    void write_inspectorWidth(int value) { write(Property::inspectorWidth, QVariant::fromValue(value)); }
    int queueHeight() const { return read(Property::queueHeight).toInt(); }
    void write_queueHeight(int value) { write(Property::queueHeight, QVariant::fromValue(value)); }
    int logHeight() const { return read(Property::logHeight).toInt(); }
    void write_logHeight(int value) { write(Property::logHeight, QVariant::fromValue(value)); }
    bool focusPreview() const { return read(Property::focusPreview).toBool(); }
    void write_focusPreview(bool value) { write(Property::focusPreview, QVariant::fromValue(value)); }
    QString exportDestinationMode() const { return read(Property::exportDestinationMode).toString(); }
    void write_exportDestinationMode(QString value) {
        write(Property::exportDestinationMode, QVariant::fromValue(value));
    }
    QString exportFolder() const { return read(Property::exportFolder).toString(); }
    void write_exportFolder(QString value) { write(Property::exportFolder, QVariant::fromValue(value)); }
    bool autoPreviewEnabled() const { return read(Property::autoPreviewEnabled).toBool(); }
    void write_autoPreviewEnabled(bool value) {
        write(Property::autoPreviewEnabled, QVariant::fromValue(value));
    }
    QVariantList languageChoices() const { return read(Property::languageChoices).toList(); }
    QString language() const { return read(Property::language).toString(); }
    void write_language(QString value) { write(Property::language, QVariant::fromValue(value)); }
    QString gpuName() const { return read(Property::gpuName).toString(); }
    QString gpuVram() const { return read(Property::gpuVram).toString(); }
    int gpuCount() const { return read(Property::gpuCount).toInt(); }
    QObject* nrImageQueue() const { return read(Property::nrImageQueue).value<QObject*>(); }
    QObject* nrVideoQueue() const { return read(Property::nrVideoQueue).value<QObject*>(); }
    QObject* upscaleImageQueue() const { return read(Property::upscaleImageQueue).value<QObject*>(); }
    QObject* upscaleVideoQueue() const { return read(Property::upscaleVideoQueue).value<QObject*>(); }
    QObject* fiQueue() const { return read(Property::fiQueue).value<QObject*>(); }
    bool previewInputIsVideo() const { return read(Property::previewInputIsVideo).toBool(); }
    bool previewOutputIsVideo() const { return read(Property::previewOutputIsVideo).toBool(); }
    int windowWidth() const { return read(Property::windowWidth).toInt(); }
    int windowHeight() const { return read(Property::windowHeight).toInt(); }
    int windowX() const { return read(Property::windowX).toInt(); }
    int windowY() const { return read(Property::windowY).toInt(); }
    bool windowMaximized() const { return read(Property::windowMaximized).toBool(); }
    QString appVersion() const { return read(Property::appVersion).toString(); }
    QString diagnosticsText() const { return read(Property::diagnosticsText).toString(); }
    QVariantList nrStyleChoices() const { return read(Property::nrStyleChoices).toList(); }
    QVariantList nrScaleChoices() const { return read(Property::nrScaleChoices).toList(); }
    QVariantList imageScalingFilterChoices() const {
        return read(Property::imageScalingFilterChoices).toList();
    }
    QVariantList videoScalingFilterChoices() const {
        return read(Property::videoScalingFilterChoices).toList();
    }
    QVariantList dlssModeChoices() const { return read(Property::dlssModeChoices).toList(); }
    QVariantList dlssPresetChoices() const { return read(Property::dlssPresetChoices).toList(); }
    QVariantList upscaleEngineChoices() const { return read(Property::upscaleEngineChoices).toList(); }
    QVariantList imageFormatChoices() const { return read(Property::imageFormatChoices).toList(); }
    QVariantList containerChoices() const { return read(Property::containerChoices).toList(); }
    QVariantList upscaleContainerChoices() const { return read(Property::upscaleContainerChoices).toList(); }
    QVariantList fiContainerChoices() const { return read(Property::fiContainerChoices).toList(); }
    QVariantList codecChoices() const { return read(Property::codecChoices).toList(); }
    QVariantList fixedQualityCodecs() const { return read(Property::fixedQualityCodecs).toList(); }
    QVariantList encodingQualityChoices() const { return read(Property::encodingQualityChoices).toList(); }
    QVariantList renameModeChoices() const { return read(Property::renameModeChoices).toList(); }
    QVariantList fiFpsChoices() const { return read(Property::fiFpsChoices).toList(); }
    QVariantList fiProviderChoices() const { return read(Property::fiProviderChoices).toList(); }
    QVariantList fsrAdapterChoices() const { return read(Property::fsrAdapterChoices).toList(); }
    QVariantList fiPreviewLengthChoices() const { return read(Property::fiPreviewLengthChoices).toList(); }
    QVariantList upscalePreviewLengthChoices() const {
        return read(Property::upscalePreviewLengthChoices).toList();
    }
    QVariantList nrPreviewLengthChoices() const { return read(Property::nrPreviewLengthChoices).toList(); }
    QVariantList imageSizeModeChoices() const { return read(Property::imageSizeModeChoices).toList(); }
    QVariantList videoSizeModeChoices() const { return read(Property::videoSizeModeChoices).toList(); }
    QVariantList imageScaleFactorChoices() const { return read(Property::imageScaleFactorChoices).toList(); }
    QVariantList videoScaleFactorChoices() const { return read(Property::videoScaleFactorChoices).toList(); }
    QVariantList vsrQualityChoices() const { return read(Property::vsrQualityChoices).toList(); }
    QVariantList hdrPrecisionChoices() const { return read(Property::hdrPrecisionChoices).toList(); }
    QVariantList aiGpuChoices() const { return read(Property::aiGpuChoices).toList(); }
    QVariantList videoGpuChoices() const { return read(Property::videoGpuChoices).toList(); }
    QString nrStyle() const { return read(Property::nrStyle).toString(); }
    void write_nrStyle(QString value) { write(Property::nrStyle, QVariant::fromValue(value)); }
    double upscalingFactor() const { return read(Property::upscalingFactor).toDouble(); }
    void write_upscalingFactor(double value) { write(Property::upscalingFactor, QVariant::fromValue(value)); }
    QString imageScalingFilter() const { return read(Property::imageScalingFilter).toString(); }
    void write_imageScalingFilter(QString value) {
        write(Property::imageScalingFilter, QVariant::fromValue(value));
    }
    QString videoScalingFilter() const { return read(Property::videoScalingFilter).toString(); }
    void write_videoScalingFilter(QString value) {
        write(Property::videoScalingFilter, QVariant::fromValue(value));
    }
    QString nrScaleMethod() const { return read(Property::nrScaleMethod).toString(); }
    void write_nrScaleMethod(QString value) { write(Property::nrScaleMethod, QVariant::fromValue(value)); }
    QString nrDlssMode() const { return read(Property::nrDlssMode).toString(); }
    void write_nrDlssMode(QString value) { write(Property::nrDlssMode, QVariant::fromValue(value)); }
    QString nrDlssPreset() const { return read(Property::nrDlssPreset).toString(); }
    void write_nrDlssPreset(QString value) { write(Property::nrDlssPreset, QVariant::fromValue(value)); }
    double nrIntensity() const { return read(Property::nrIntensity).toDouble(); }
    void write_nrIntensity(double value) { write(Property::nrIntensity, QVariant::fromValue(value)); }
    int nrPasses() const { return read(Property::nrPasses).toInt(); }
    void write_nrPasses(int value) { write(Property::nrPasses, QVariant::fromValue(value)); }
    QVariantList opticalFlowQualityChoices() const {
        return read(Property::opticalFlowQualityChoices).toList();
    }
    QString opticalFlowQuality() const { return read(Property::opticalFlowQuality).toString(); }
    void write_opticalFlowQuality(QString value) {
        write(Property::opticalFlowQuality, QVariant::fromValue(value));
    }
    double localToneStrength() const { return read(Property::localToneStrength).toDouble(); }
    void write_localToneStrength(double value) {
        write(Property::localToneStrength, QVariant::fromValue(value));
    }
    double localStructureStrength() const { return read(Property::localStructureStrength).toDouble(); }
    void write_localStructureStrength(double value) {
        write(Property::localStructureStrength, QVariant::fromValue(value));
    }
    double skinStructureStrength() const { return read(Property::skinStructureStrength).toDouble(); }
    void write_skinStructureStrength(double value) {
        write(Property::skinStructureStrength, QVariant::fromValue(value));
    }
    bool automaticMask() const { return read(Property::automaticMask).toBool(); }
    void write_automaticMask(bool value) { write(Property::automaticMask, QVariant::fromValue(value)); }
    QString customMaskStatus() const { return read(Property::customMaskStatus).toString(); }
    QString imageFormat() const { return read(Property::imageFormat).toString(); }
    void write_imageFormat(QString value) { write(Property::imageFormat, QVariant::fromValue(value)); }
    int imageQuality() const { return read(Property::imageQuality).toInt(); }
    void write_imageQuality(int value) { write(Property::imageQuality, QVariant::fromValue(value)); }
    int imageBitDepth() const { return read(Property::imageBitDepth).toInt(); }
    void write_imageBitDepth(int value) { write(Property::imageBitDepth, QVariant::fromValue(value)); }
    QString imageRenameMode() const { return read(Property::imageRenameMode).toString(); }
    void write_imageRenameMode(QString value) {
        write(Property::imageRenameMode, QVariant::fromValue(value));
    }
    QString imageCustomSuffix() const { return read(Property::imageCustomSuffix).toString(); }
    void write_imageCustomSuffix(QString value) {
        write(Property::imageCustomSuffix, QVariant::fromValue(value));
    }
    QString videoCodec() const { return read(Property::videoCodec).toString(); }
    void write_videoCodec(QString value) { write(Property::videoCodec, QVariant::fromValue(value)); }
    QString videoContainer() const { return read(Property::videoContainer).toString(); }
    void write_videoContainer(QString value) { write(Property::videoContainer, QVariant::fromValue(value)); }
    QString videoQuality() const { return read(Property::videoQuality).toString(); }
    void write_videoQuality(QString value) { write(Property::videoQuality, QVariant::fromValue(value)); }
    bool videoHdrMode() const { return read(Property::videoHdrMode).toBool(); }
    void write_videoHdrMode(bool value) { write(Property::videoHdrMode, QVariant::fromValue(value)); }
    QString videoRenameMode() const { return read(Property::videoRenameMode).toString(); }
    void write_videoRenameMode(QString value) {
        write(Property::videoRenameMode, QVariant::fromValue(value));
    }
    QString videoCustomSuffix() const { return read(Property::videoCustomSuffix).toString(); }
    void write_videoCustomSuffix(QString value) {
        write(Property::videoCustomSuffix, QVariant::fromValue(value));
    }
    QString upscaleImageEngine() const { return read(Property::upscaleImageEngine).toString(); }
    void write_upscaleImageEngine(QString value) {
        write(Property::upscaleImageEngine, QVariant::fromValue(value));
    }
    QString upscaleImageDlssMode() const { return read(Property::upscaleImageDlssMode).toString(); }
    void write_upscaleImageDlssMode(QString value) {
        write(Property::upscaleImageDlssMode, QVariant::fromValue(value));
    }
    QString upscaleImageDlssPreset() const { return read(Property::upscaleImageDlssPreset).toString(); }
    void write_upscaleImageDlssPreset(QString value) {
        write(Property::upscaleImageDlssPreset, QVariant::fromValue(value));
    }
    int upscaleImageVsrQuality() const { return read(Property::upscaleImageVsrQuality).toInt(); }
    void write_upscaleImageVsrQuality(int value) {
        write(Property::upscaleImageVsrQuality, QVariant::fromValue(value));
    }
    QString upscaleImageSizeMode() const { return read(Property::upscaleImageSizeMode).toString(); }
    void write_upscaleImageSizeMode(QString value) {
        write(Property::upscaleImageSizeMode, QVariant::fromValue(value));
    }
    double upscaleImageScaleFactor() const { return read(Property::upscaleImageScaleFactor).toDouble(); }
    void write_upscaleImageScaleFactor(double value) {
        write(Property::upscaleImageScaleFactor, QVariant::fromValue(value));
    }
    int upscaleImageWidth() const { return read(Property::upscaleImageWidth).toInt(); }
    void write_upscaleImageWidth(int value) {
        write(Property::upscaleImageWidth, QVariant::fromValue(value));
    }
    int upscaleImageHeight() const { return read(Property::upscaleImageHeight).toInt(); }
    void write_upscaleImageHeight(int value) {
        write(Property::upscaleImageHeight, QVariant::fromValue(value));
    }
    bool upscaleImageAspectLock() const { return read(Property::upscaleImageAspectLock).toBool(); }
    void write_upscaleImageAspectLock(bool value) {
        write(Property::upscaleImageAspectLock, QVariant::fromValue(value));
    }
    QString upscaleImageOutputFormat() const { return read(Property::upscaleImageOutputFormat).toString(); }
    void write_upscaleImageOutputFormat(QString value) {
        write(Property::upscaleImageOutputFormat, QVariant::fromValue(value));
    }
    int upscaleImageQuality() const { return read(Property::upscaleImageQuality).toInt(); }
    void write_upscaleImageQuality(int value) {
        write(Property::upscaleImageQuality, QVariant::fromValue(value));
    }
    QString upscaleEngine() const { return read(Property::upscaleEngine).toString(); }
    void write_upscaleEngine(QString value) { write(Property::upscaleEngine, QVariant::fromValue(value)); }
    QString upscaleDlssMode() const { return read(Property::upscaleDlssMode).toString(); }
    void write_upscaleDlssMode(QString value) {
        write(Property::upscaleDlssMode, QVariant::fromValue(value));
    }
    QString upscaleDlssPreset() const { return read(Property::upscaleDlssPreset).toString(); }
    void write_upscaleDlssPreset(QString value) {
        write(Property::upscaleDlssPreset, QVariant::fromValue(value));
    }
    QString upscaleOpticalFlowQuality() const { return read(Property::upscaleOpticalFlowQuality).toString(); }
    void write_upscaleOpticalFlowQuality(QString value) {
        write(Property::upscaleOpticalFlowQuality, QVariant::fromValue(value));
    }
    bool upscaleVsrEnabled() const { return read(Property::upscaleVsrEnabled).toBool(); }
    void write_upscaleVsrEnabled(bool value) {
        write(Property::upscaleVsrEnabled, QVariant::fromValue(value));
    }
    int upscaleVsrQuality() const { return read(Property::upscaleVsrQuality).toInt(); }
    void write_upscaleVsrQuality(int value) {
        write(Property::upscaleVsrQuality, QVariant::fromValue(value));
    }
    QString upscaleSizeMode() const { return read(Property::upscaleSizeMode).toString(); }
    void write_upscaleSizeMode(QString value) {
        write(Property::upscaleSizeMode, QVariant::fromValue(value));
    }
    double upscaleScaleFactor() const { return read(Property::upscaleScaleFactor).toDouble(); }
    void write_upscaleScaleFactor(double value) {
        write(Property::upscaleScaleFactor, QVariant::fromValue(value));
    }
    int upscaleWidth() const { return read(Property::upscaleWidth).toInt(); }
    void write_upscaleWidth(int value) { write(Property::upscaleWidth, QVariant::fromValue(value)); }
    int upscaleHeight() const { return read(Property::upscaleHeight).toInt(); }
    void write_upscaleHeight(int value) { write(Property::upscaleHeight, QVariant::fromValue(value)); }
    bool upscaleAspectLock() const { return read(Property::upscaleAspectLock).toBool(); }
    void write_upscaleAspectLock(bool value) {
        write(Property::upscaleAspectLock, QVariant::fromValue(value));
    }
    bool upscaleHdrEnabled() const { return read(Property::upscaleHdrEnabled).toBool(); }
    void write_upscaleHdrEnabled(bool value) {
        write(Property::upscaleHdrEnabled, QVariant::fromValue(value));
    }
    int upscaleHdrContrast() const { return read(Property::upscaleHdrContrast).toInt(); }
    void write_upscaleHdrContrast(int value) {
        write(Property::upscaleHdrContrast, QVariant::fromValue(value));
    }
    int upscaleHdrSaturation() const { return read(Property::upscaleHdrSaturation).toInt(); }
    void write_upscaleHdrSaturation(int value) {
        write(Property::upscaleHdrSaturation, QVariant::fromValue(value));
    }
    int upscaleHdrMiddleGray() const { return read(Property::upscaleHdrMiddleGray).toInt(); }
    void write_upscaleHdrMiddleGray(int value) {
        write(Property::upscaleHdrMiddleGray, QVariant::fromValue(value));
    }
    int upscaleHdrPeakLuminance() const { return read(Property::upscaleHdrPeakLuminance).toInt(); }
    void write_upscaleHdrPeakLuminance(int value) {
        write(Property::upscaleHdrPeakLuminance, QVariant::fromValue(value));
    }
    QString upscaleHdrPrecision() const { return read(Property::upscaleHdrPrecision).toString(); }
    void write_upscaleHdrPrecision(QString value) {
        write(Property::upscaleHdrPrecision, QVariant::fromValue(value));
    }
    QString upscaleCodec() const { return read(Property::upscaleCodec).toString(); }
    void write_upscaleCodec(QString value) { write(Property::upscaleCodec, QVariant::fromValue(value)); }
    QString upscaleContainer() const { return read(Property::upscaleContainer).toString(); }
    void write_upscaleContainer(QString value) {
        write(Property::upscaleContainer, QVariant::fromValue(value));
    }
    QString upscaleQuality() const { return read(Property::upscaleQuality).toString(); }
    void write_upscaleQuality(QString value) { write(Property::upscaleQuality, QVariant::fromValue(value)); }
    QString upscaleImageRenameMode() const { return read(Property::upscaleImageRenameMode).toString(); }
    void write_upscaleImageRenameMode(QString value) {
        write(Property::upscaleImageRenameMode, QVariant::fromValue(value));
    }
    QString upscaleImageCustomSuffix() const { return read(Property::upscaleImageCustomSuffix).toString(); }
    void write_upscaleImageCustomSuffix(QString value) {
        write(Property::upscaleImageCustomSuffix, QVariant::fromValue(value));
    }
    QString upscaleRenameMode() const { return read(Property::upscaleRenameMode).toString(); }
    void write_upscaleRenameMode(QString value) {
        write(Property::upscaleRenameMode, QVariant::fromValue(value));
    }
    QString upscaleCustomSuffix() const { return read(Property::upscaleCustomSuffix).toString(); }
    void write_upscaleCustomSuffix(QString value) {
        write(Property::upscaleCustomSuffix, QVariant::fromValue(value));
    }
    QString fiFrameRateMode() const { return read(Property::fiFrameRateMode).toString(); }
    void write_fiFrameRateMode(QString value) { write(Property::fiFrameRateMode, QVariant::fromValue(value)); }
    int fiMultiplier() const { return read(Property::fiMultiplier).toInt(); }
    void write_fiMultiplier(int value) { write(Property::fiMultiplier, QVariant::fromValue(value)); }
    QString fiTargetFps() const { return read(Property::fiTargetFps).toString(); }
    void write_fiTargetFps(QString value) { write(Property::fiTargetFps, QVariant::fromValue(value)); }
    QString fiProvider() const { return read(Property::fiProvider).toString(); }
    void write_fiProvider(QString value) { write(Property::fiProvider, QVariant::fromValue(value)); }
    QString fsrFrameRateMode() const { return read(Property::fsrFrameRateMode).toString(); }
    void write_fsrFrameRateMode(QString value) { write(Property::fsrFrameRateMode, QVariant::fromValue(value)); }
    int fsrMultiplier() const { return read(Property::fsrMultiplier).toInt(); }
    void write_fsrMultiplier(int value) { write(Property::fsrMultiplier, QVariant::fromValue(value)); }
    QString fsrTargetFps() const { return read(Property::fsrTargetFps).toString(); }
    void write_fsrTargetFps(QString value) { write(Property::fsrTargetFps, QVariant::fromValue(value)); }
    QString fsrAdapterId() const { return read(Property::fsrAdapterId).toString(); }
    void write_fsrAdapterId(QString value) { write(Property::fsrAdapterId, QVariant::fromValue(value)); }
    QString fsrStatus() const { return read(Property::fsrStatus).toString(); }
    QString fiOpticalFlowQuality() const { return read(Property::fiOpticalFlowQuality).toString(); }
    void write_fiOpticalFlowQuality(QString value) {
        write(Property::fiOpticalFlowQuality, QVariant::fromValue(value));
    }
    QString fiCodec() const { return read(Property::fiCodec).toString(); }
    void write_fiCodec(QString value) { write(Property::fiCodec, QVariant::fromValue(value)); }
    QString fiContainer() const { return read(Property::fiContainer).toString(); }
    void write_fiContainer(QString value) { write(Property::fiContainer, QVariant::fromValue(value)); }
    QString fiQuality() const { return read(Property::fiQuality).toString(); }
    void write_fiQuality(QString value) { write(Property::fiQuality, QVariant::fromValue(value)); }
    QString fiPreviewLength() const { return read(Property::fiPreviewLength).toString(); }
    void write_fiPreviewLength(QString value) {
        write(Property::fiPreviewLength, QVariant::fromValue(value));
    }
    bool fiHdrMode() const { return read(Property::fiHdrMode).toBool(); }
    void write_fiHdrMode(bool value) { write(Property::fiHdrMode, QVariant::fromValue(value)); }
    QString fiRenameMode() const { return read(Property::fiRenameMode).toString(); }
    void write_fiRenameMode(QString value) { write(Property::fiRenameMode, QVariant::fromValue(value)); }
    QString fiCustomSuffix() const { return read(Property::fiCustomSuffix).toString(); }
    void write_fiCustomSuffix(QString value) { write(Property::fiCustomSuffix, QVariant::fromValue(value)); }
    bool videoHdrSupported() const { return read(Property::videoHdrSupported).toBool(); }
    bool fiHdrSupported() const { return read(Property::fiHdrSupported).toBool(); }
    bool upscaleHdrSupported() const { return read(Property::upscaleHdrSupported).toBool(); }
    QString aiGpuUuid() const { return read(Property::aiGpuUuid).toString(); }
    void write_aiGpuUuid(QString value) { write(Property::aiGpuUuid, QVariant::fromValue(value)); }
    QString videoGpuUuid() const { return read(Property::videoGpuUuid).toString(); }
    void write_videoGpuUuid(QString value) { write(Property::videoGpuUuid, QVariant::fromValue(value)); }
    QString renderValidationMessage() const { return read(Property::renderValidationMessage).toString(); }
    QString frameGenerationEstimate() const { return read(Property::frameGenerationEstimate).toString(); }
    QString rtxSuperResolutionEstimate() const {
        return read(Property::rtxSuperResolutionEstimate).toString();
    }
    QString outputEstimate() const { return read(Property::outputEstimate).toString(); }
    QString currentLogPath() const { return read(Property::currentLogPath).toString(); }
    QString fullLog() const { return read(Property::fullLog).toString(); }
    QVariantMap vfxSettings() const { return read(Property::vfxSettings).toMap(); }
    QVariantMap rrSettings() const { return read(Property::rrSettings).toMap(); }
    QVariantMap reshadeSettings() const { return read(Property::reshadeSettings).toMap(); }
    QString reshadeStatus() const { return read(Property::reshadeStatus).toString(); }
    QVariantMap reshadePresetInfo() const { return read(Property::reshadePresetInfo).toMap(); }
    Q_INVOKABLE void setRrSetting(QString key, QVariant value) { execute(Action::setRrSetting, {key,value}); }
    Q_INVOKABLE void setReShadeSetting(QString key, QVariant value) { execute(Action::setReShadeSetting, {key, value}); }
    Q_INVOKABLE void reloadReShadePreset() { execute(Action::reloadReShadePreset, {}); }
    Q_INVOKABLE QString saveReShadePresetCopy(QString targetPath) {
        return execute(Action::saveReShadePresetCopy, {targetPath}).toString();
    }
    Q_INVOKABLE void setReShadeParameter(QString key, QVariant value) {
        execute(Action::setReShadeParameter, {key, value});
    }
    Q_INVOKABLE void resetReShadeParameters() { execute(Action::resetReShadeParameters, {}); }
    Q_INVOKABLE void setReShadeTechniqueEnabled(QString name, bool enabled) {
        execute(Action::setReShadeTechniqueEnabled, {name, enabled});
    }
    Q_INVOKABLE void moveReShadeTechnique(int fromIndex, int toIndex) {
        execute(Action::moveReShadeTechnique, {fromIndex, toIndex});
    }
    QString vfxStatus() const { return read(Property::vfxStatus).toString(); }
    Q_INVOKABLE void setVfxSetting(QString key, QVariant value) { execute(Action::setVfxSetting, {key,value}); }
    Q_INVOKABLE void setLutFile(QString a0) { execute(Action::setLutFile, {QVariant::fromValue(a0)}); }
    Q_INVOKABLE void setLutAdjustment(QString a0, double a1) {
        execute(Action::setLutAdjustment, {QVariant::fromValue(a0), QVariant::fromValue(a1)});
    }
    Q_INVOKABLE void resetLutAdjustments() { execute(Action::resetLutAdjustments, {}); }
    Q_INVOKABLE void autoAdjustLut() { execute(Action::autoAdjustLut, {}); }
    Q_INVOKABLE void autoAdjustLut(double a0) { execute(Action::autoAdjustLut, {QVariant::fromValue(a0)}); }
    Q_INVOKABLE void autoAdjustLutFixedFrames() { execute(Action::autoAdjustLutFixedFrames, {}); }
    Q_INVOKABLE void setLutReferenceImage(QString a0) {
        execute(Action::setLutReferenceImage, {QVariant::fromValue(a0)});
    }
    Q_INVOKABLE void saveLut(QString a0) { execute(Action::saveLut, {QVariant::fromValue(a0)}); }
    Q_INVOKABLE void setWorkflowStageEnabled(QString a0, bool a1) {
        execute(Action::setWorkflowStageEnabled, {QVariant::fromValue(a0), QVariant::fromValue(a1)});
    }
    Q_INVOKABLE void moveWorkflowStage(QString a0, int a1) {
        execute(Action::moveWorkflowStage, {QVariant::fromValue(a0), QVariant::fromValue(a1)});
    }
    Q_INVOKABLE void saveWindowLayout(int a0, int a1, int a2, int a3, bool a4) {
        execute(Action::saveWindowLayout,
                {QVariant::fromValue(a0), QVariant::fromValue(a1), QVariant::fromValue(a2),
                 QVariant::fromValue(a3), QVariant::fromValue(a4)});
    }
    Q_INVOKABLE void initializeRuntime() { execute(Action::initializeRuntime, {}); }
    Q_INVOKABLE void selectCustomMask(QString a0) {
        execute(Action::selectCustomMask, {QVariant::fromValue(a0)});
    }
    Q_INVOKABLE void clearCustomMask() { execute(Action::clearCustomMask, {}); }
    Q_INVOKABLE void resetToDefaults() { execute(Action::resetToDefaults, {}); }
    Q_INVOKABLE void resetTabSettings(QString a0) {
        execute(Action::resetTabSettings, {QVariant::fromValue(a0)});
    }
    Q_INVOKABLE QString exportPreset(QString a0) {
        return execute(Action::exportPreset, {QVariant::fromValue(a0)}).toString();
    }
    Q_INVOKABLE QString exportPresetTo(QString a0, QString a1) {
        return execute(Action::exportPresetTo, {QVariant::fromValue(a0), QVariant::fromValue(a1)}).toString();
    }
    Q_INVOKABLE bool importPreset(QString a0) {
        return execute(Action::importPreset, {QVariant::fromValue(a0)}).toBool();
    }
    Q_INVOKABLE void addFiles(QVariantList a0) { execute(Action::addFiles, {QVariant::fromValue(a0)}); }
    Q_INVOKABLE void _sweep_temp_caches(int a0) {
        execute(Action::_sweep_temp_caches, {QVariant::fromValue(a0)});
    }
    Q_INVOKABLE void selectQueueItem(int a0) { execute(Action::selectQueueItem, {QVariant::fromValue(a0)}); }
    Q_INVOKABLE void removeQueueItem(int a0) { execute(Action::removeQueueItem, {QVariant::fromValue(a0)}); }
    Q_INVOKABLE void clearActiveQueue() { execute(Action::clearActiveQueue, {}); }
    Q_INVOKABLE void clearSelectedPreviewItem() { execute(Action::clearSelectedPreviewItem, {}); }
    Q_INVOKABLE void showSelectedInExplorer() { execute(Action::showSelectedInExplorer, {}); }
    Q_INVOKABLE void showInExplorer(QString path) { execute(Action::showInExplorer, {path}); }
    Q_INVOKABLE void clearCompletedQueueItems() { execute(Action::clearCompletedQueueItems, {}); }
    Q_INVOKABLE void retryFailedQueueItems() { execute(Action::retryFailedQueueItems, {}); }
    Q_INVOKABLE void startActiveBatch() { execute(Action::startActiveBatch, {}); }
    Q_INVOKABLE bool requestActiveBatchExport() {
        return execute(Action::requestActiveBatchExport, {}).toBool();
    }
    Q_INVOKABLE QString startActiveBatchWithExport(QString a0, QString a1, QString a2, QString a3,
                                                   QString a4) {
        return execute(Action::startActiveBatchWithExport,
                       {QVariant::fromValue(a0), QVariant::fromValue(a1), QVariant::fromValue(a2),
                        QVariant::fromValue(a3), QVariant::fromValue(a4)})
            .toString();
    }
    Q_INVOKABLE void pauseExport() { execute(Action::pauseExport, {}); }
    Q_INVOKABLE void toggleExportPause() { execute(Action::toggleExportPause, {}); }
    Q_INVOKABLE void stopActiveBatch() { execute(Action::stopActiveBatch, {}); }
    Q_INVOKABLE void renderPreview() { execute(Action::renderPreview, {}); }
    Q_INVOKABLE void notePlayheadMs(double a0) { execute(Action::notePlayheadMs, {QVariant::fromValue(a0)}); }
    Q_INVOKABLE void renderPreviewAt(double a0) {
        execute(Action::renderPreviewAt, {QVariant::fromValue(a0)});
    }
    Q_INVOKABLE void schedulePreviewAt(double a0, bool dragging = false) {
        execute(Action::schedulePreviewAt, {QVariant::fromValue(a0), QVariant::fromValue(dragging)});
    }
    Q_INVOKABLE void refreshVideoPoster(double a0) {
        execute(Action::refreshVideoPoster, {QVariant::fromValue(a0)});
    }
    Q_INVOKABLE void shutdown() { execute(Action::shutdown, {}); }
    Q_INVOKABLE void openFolder(QString a0) { execute(Action::openFolder, {QVariant::fromValue(a0)}); }
    Q_INVOKABLE void openThirdPartyNotices() { execute(Action::openThirdPartyNotices, {}); }
    Q_INVOKABLE void copyToClipboard(QString a0) {
        execute(Action::copyToClipboard, {QVariant::fromValue(a0)});
    }
    Q_INVOKABLE void pasteFromClipboard() { execute(Action::pasteFromClipboard, {}); }
    Q_INVOKABLE void _append_backend_log(QString a0, QString a1, QString a2) {
        execute(Action::_append_backend_log,
                {QVariant::fromValue(a0), QVariant::fromValue(a1), QVariant::fromValue(a2)});
    }
    Q_INVOKABLE void clearLogDisplay() { execute(Action::clearLogDisplay, {}); }
    qint64 processingCacheBytes() const { return read(Property::processingCacheBytes).toLongLong(); }
    qint64 processingCacheReclaimableBytes() const { return read(Property::processingCacheReclaimableBytes).toLongLong(); }
    bool processingCacheBusy() const { return read(Property::processingCacheBusy).toBool(); }
    bool processingCacheCleaning() const { return read(Property::processingCacheCleaning).toBool(); }
    bool processingCacheReady() const { return read(Property::processingCacheReady).toBool(); }
    bool canCleanProcessingCache() const { return read(Property::canCleanProcessingCache).toBool(); }
    QString processingCacheSizeText() const { return read(Property::processingCacheSizeText).toString(); }
    QString processingCacheStatus() const { return read(Property::processingCacheStatus).toString(); }
    QString processingCacheDetails() const { return read(Property::processingCacheDetails).toString(); }
    Q_INVOKABLE void refreshProcessingCache() { execute(Action::refreshProcessingCache, {}); }
    Q_INVOKABLE void cleanProcessingCache() { execute(Action::cleanProcessingCache, {}); }
  signals:
    void reshadeStatusChanged();
    void reshadePresetInfoChanged();
    void reshadeParametersReset();
    void processingCacheChanged();
    void activeTabChanged();
    void nrModeChanged();
    void upscaleModeChanged();
    void mediaPreviewChanged();
    void isProcessingChanged();
    void overallProgressChanged();
    void statusMessageChanged();
    void exportPhaseChanged();
    void splitPositionChanged();
    void hasOutputPreviewChanged();
    void previewInputUrlChanged();
    void previewOutputUrlChanged();
    void previewPosterUrlChanged();
    void previewSourceChanged();
    void previewRangesChanged();
    void inputInfoTextChanged();
    void outputInfoTextChanged();
    void logAppended(QString a0);
    void backendLogReceived(QString a0, QString a1, QString a2);
    void operationStateChanged();
    void runtimeStateChanged();
    void layoutChanged();
    void sourceSizeChanged();
    void outputSizeChanged();
    void batchExportRequested(QString a0);
    void exportPrefsChanged();
    void workflowStagesChanged();
    void estimateChanged();
    void settingsUpdated();
    void vfxStatusChanged();
    void gpuChoicesChanged();
    void presetStatusChanged();
    void autoPreviewChanged();
    void previewBusyChanged();
    void languageChanged();
    void lutSaveChanged();
    void lutAutoChanged();
    void lutReferenceChanged();

  protected:
    virtual QVariant read(Property property) const = 0;
    virtual void write(Property property, const QVariant& value) = 0;
    virtual QVariant execute(Action action, const QVariantList& arguments) = 0;
    void notify(Property property) {
        switch (property) {
        case Property::activeTab:
            emit activeTabChanged();
            break;
        case Property::nrMode:
            emit nrModeChanged();
            break;
        case Property::workflowStages:
            emit workflowStagesChanged();
            break;
        case Property::reshadeSettings:
        case Property::reshadeStatus:
        case Property::reshadePresetInfo:
            emit settingsUpdated();
            break;
        case Property::sharpeningMethodChoices:
            emit settingsUpdated();
            break;
        case Property::ffmpegDeviceChoices:
            emit gpuChoicesChanged();
            break;
        case Property::ffmpegDevice:
            emit settingsUpdated();
            break;
        case Property::ffmpegDeviceStatus:
            emit settingsUpdated();
            break;
        case Property::sharpeningMethod:
            emit settingsUpdated();
            break;
        case Property::casSharpness:
            emit settingsUpdated();
            break;
        case Property::grainAmount:
            emit settingsUpdated();
            break;
        case Property::grainSize:
            emit settingsUpdated();
            break;
        case Property::grainColor:
            emit settingsUpdated();
            break;
        case Property::grainResponse:
            emit settingsUpdated();
            break;
        case Property::grainSeed:
            emit settingsUpdated();
            break;
        case Property::grainAnimated:
            emit settingsUpdated();
            break;
        case Property::lutFile:
            emit settingsUpdated();
            break;
        case Property::lutResolutionChoices:
            emit settingsUpdated();
            break;
        case Property::lutResolution:
            emit settingsUpdated();
            break;
        case Property::lutAdjustmentValues:
            emit settingsUpdated();
            break;
        case Property::lutAutoBusy:
            emit lutAutoChanged();
            break;
        case Property::lutAutoMode:
            emit lutAutoChanged();
            break;
        case Property::lutReferenceImage:
            emit settingsUpdated();
            break;
        case Property::lutReferenceBusy:
            emit lutReferenceChanged();
            break;
        case Property::lutReferenceStatus:
            emit lutReferenceChanged();
            break;
        case Property::lutSaveBusy:
            emit lutSaveChanged();
            break;
        case Property::lutSaveStatus:
            emit lutSaveChanged();
            break;
        case Property::upscaleMode:
            emit upscaleModeChanged();
            break;
        case Property::upscalePreviewLength:
            emit settingsUpdated();
            break;
        case Property::nrPreviewLength:
            emit settingsUpdated();
            break;
        case Property::isProcessing:
            emit isProcessingChanged();
            break;
        case Property::overallProgress:
            emit overallProgressChanged();
            break;
        case Property::canPauseExport:
            emit operationStateChanged();
            break;
        case Property::exportPaused:
            emit exportPhaseChanged();
            break;
        case Property::exportPauseRequested:
            emit exportPhaseChanged();
            break;
        case Property::exportPhaseText:
            emit exportPhaseChanged();
            break;
        case Property::statusMessage:
            emit statusMessageChanged();
            break;
        case Property::previewError:
            emit mediaPreviewChanged();
            break;
        case Property::splitPosition:
            emit splitPositionChanged();
            break;
        case Property::hasOutputPreview:
            emit hasOutputPreviewChanged();
            break;
        case Property::previewInputUrl:
            emit previewInputUrlChanged();
            break;
        case Property::previewOutputUrl:
            emit previewOutputUrlChanged();
            break;
        case Property::previewPosterUrl:
            emit previewPosterUrlChanged();
            break;
        case Property::previewSourceSeconds:
            emit previewSourceChanged();
            break;
        case Property::previewSourceFrame:
            emit previewSourceChanged();
            break;
        case Property::previewRenderedRanges:
            emit previewRangesChanged();
            break;
        case Property::inputInfoText:
            emit inputInfoTextChanged();
            break;
        case Property::outputInfoText:
            emit outputInfoTextChanged();
            break;
        case Property::presetStatus:
            emit presetStatusChanged();
            break;
        case Property::previewBusy:
            emit previewBusyChanged();
            break;
        case Property::operationState:
            emit operationStateChanged();
            break;
        case Property::fastColorPreviewActive:
            emit operationStateChanged();
            break;
        case Property::canModifyQueue:
            emit operationStateChanged();
            break;
        case Property::canPreview:
            emit operationStateChanged();
            break;
        case Property::canRender:
            emit operationStateChanged();
            break;
        case Property::canStop:
            emit operationStateChanged();
            break;
        case Property::runtimeState:
            emit runtimeStateChanged();
            break;
        case Property::runtimeError:
            emit runtimeStateChanged();
            break;
        case Property::startupWarnings:
            emit runtimeStateChanged();
            break;
        case Property::sourceWidth:
            emit sourceSizeChanged();
            break;
        case Property::sourceHeight:
            emit sourceSizeChanged();
            break;
        case Property::sourceFps:
            emit sourceSizeChanged();
            break;
        case Property::sourceDuration:
            emit sourceSizeChanged();
            break;
        case Property::outputWidth:
            emit outputSizeChanged();
            break;
        case Property::outputHeight:
            emit outputSizeChanged();
            break;
        case Property::inspectorWidth:
            emit layoutChanged();
            break;
        case Property::queueHeight:
            emit layoutChanged();
            break;
        case Property::logHeight:
            emit layoutChanged();
            break;
        case Property::focusPreview:
            emit layoutChanged();
            break;
        case Property::exportDestinationMode:
            emit exportPrefsChanged();
            break;
        case Property::exportFolder:
            emit exportPrefsChanged();
            break;
        case Property::autoPreviewEnabled:
            emit autoPreviewChanged();
            break;
        case Property::languageChoices:
            emit settingsUpdated();
            break;
        case Property::language:
            emit languageChanged();
            break;
        case Property::gpuName:
            emit runtimeStateChanged();
            break;
        case Property::gpuVram:
            emit runtimeStateChanged();
            break;
        case Property::gpuCount:
            emit runtimeStateChanged();
            break;
        case Property::nrImageQueue:
            emit settingsUpdated();
            break;
        case Property::nrVideoQueue:
            emit settingsUpdated();
            break;
        case Property::upscaleImageQueue:
            emit settingsUpdated();
            break;
        case Property::upscaleVideoQueue:
            emit settingsUpdated();
            break;
        case Property::fiQueue:
            emit settingsUpdated();
            break;
        case Property::previewInputIsVideo:
            emit mediaPreviewChanged();
            break;
        case Property::previewOutputIsVideo:
            emit mediaPreviewChanged();
            break;
        case Property::windowWidth:
            emit layoutChanged();
            break;
        case Property::windowHeight:
            emit layoutChanged();
            break;
        case Property::windowX:
            emit layoutChanged();
            break;
        case Property::windowY:
            emit layoutChanged();
            break;
        case Property::windowMaximized:
            emit layoutChanged();
            break;
        case Property::appVersion:
            emit settingsUpdated();
            break;
        case Property::diagnosticsText:
            emit runtimeStateChanged();
            break;
        case Property::nrStyleChoices:
            emit languageChanged();
            break;
        case Property::nrScaleChoices:
            emit settingsUpdated();
            break;
        case Property::imageScalingFilterChoices:
            emit settingsUpdated();
            break;
        case Property::videoScalingFilterChoices:
            emit settingsUpdated();
            break;
        case Property::dlssModeChoices:
            emit settingsUpdated();
            break;
        case Property::dlssPresetChoices:
            emit settingsUpdated();
            break;
        case Property::upscaleEngineChoices:
            emit settingsUpdated();
            break;
        case Property::imageFormatChoices:
            emit settingsUpdated();
            break;
        case Property::containerChoices:
            emit settingsUpdated();
            break;
        case Property::upscaleContainerChoices:
            emit settingsUpdated();
            break;
        case Property::fiContainerChoices:
            emit settingsUpdated();
            break;
        case Property::codecChoices:
            emit settingsUpdated();
            break;
        case Property::fixedQualityCodecs:
            emit settingsUpdated();
            break;
        case Property::encodingQualityChoices:
            emit settingsUpdated();
            break;
        case Property::renameModeChoices:
            emit settingsUpdated();
            break;
        case Property::fiFpsChoices:
            emit settingsUpdated();
            break;
        case Property::fiProviderChoices:
            emit settingsUpdated();
            break;
        case Property::fsrAdapterChoices:
            emit gpuChoicesChanged();
            break;
        case Property::fiPreviewLengthChoices:
            emit languageChanged();
            break;
        case Property::upscalePreviewLengthChoices:
            emit languageChanged();
            break;
        case Property::nrPreviewLengthChoices:
            emit languageChanged();
            break;
        case Property::imageSizeModeChoices:
            emit settingsUpdated();
            break;
        case Property::videoSizeModeChoices:
            emit settingsUpdated();
            break;
        case Property::imageScaleFactorChoices:
            emit settingsUpdated();
            break;
        case Property::videoScaleFactorChoices:
            emit settingsUpdated();
            break;
        case Property::vsrQualityChoices:
            emit settingsUpdated();
            break;
        case Property::hdrPrecisionChoices:
            emit settingsUpdated();
            break;
        case Property::aiGpuChoices:
            emit gpuChoicesChanged();
            break;
        case Property::videoGpuChoices:
            emit gpuChoicesChanged();
            break;
        case Property::nrStyle:
            emit settingsUpdated();
            break;
        case Property::upscalingFactor:
            emit settingsUpdated();
            break;
        case Property::imageScalingFilter:
            emit settingsUpdated();
            break;
        case Property::videoScalingFilter:
            emit settingsUpdated();
            break;
        case Property::nrScaleMethod:
            emit settingsUpdated();
            break;
        case Property::nrDlssMode:
            emit settingsUpdated();
            break;
        case Property::nrDlssPreset:
            emit settingsUpdated();
            break;
        case Property::nrIntensity:
            emit settingsUpdated();
            break;
        case Property::nrPasses:
            emit settingsUpdated();
            break;
        case Property::opticalFlowQualityChoices:
            emit settingsUpdated();
            break;
        case Property::opticalFlowQuality:
            emit settingsUpdated();
            break;
        case Property::localToneStrength:
            emit settingsUpdated();
            break;
        case Property::localStructureStrength:
            emit settingsUpdated();
            break;
        case Property::skinStructureStrength:
            emit settingsUpdated();
            break;
        case Property::automaticMask:
            emit settingsUpdated();
            break;
        case Property::customMaskStatus:
            emit settingsUpdated();
            break;
        case Property::imageFormat:
            emit settingsUpdated();
            break;
        case Property::imageQuality:
            emit settingsUpdated();
            break;
        case Property::imageBitDepth:
            emit settingsUpdated();
            break;
        case Property::imageRenameMode:
            emit settingsUpdated();
            break;
        case Property::imageCustomSuffix:
            emit settingsUpdated();
            break;
        case Property::videoCodec:
            emit settingsUpdated();
            break;
        case Property::videoContainer:
            emit settingsUpdated();
            break;
        case Property::videoQuality:
            emit settingsUpdated();
            break;
        case Property::videoHdrMode:
            emit settingsUpdated();
            break;
        case Property::videoRenameMode:
            emit settingsUpdated();
            break;
        case Property::videoCustomSuffix:
            emit settingsUpdated();
            break;
        case Property::upscaleImageEngine:
            emit settingsUpdated();
            break;
        case Property::upscaleImageDlssMode:
            emit settingsUpdated();
            break;
        case Property::upscaleImageDlssPreset:
            emit settingsUpdated();
            break;
        case Property::upscaleImageVsrQuality:
            emit settingsUpdated();
            break;
        case Property::upscaleImageSizeMode:
            emit settingsUpdated();
            break;
        case Property::upscaleImageScaleFactor:
            emit settingsUpdated();
            break;
        case Property::upscaleImageWidth:
            emit settingsUpdated();
            break;
        case Property::upscaleImageHeight:
            emit settingsUpdated();
            break;
        case Property::upscaleImageAspectLock:
            emit settingsUpdated();
            break;
        case Property::upscaleImageOutputFormat:
            emit settingsUpdated();
            break;
        case Property::upscaleImageQuality:
            emit settingsUpdated();
            break;
        case Property::upscaleEngine:
            emit settingsUpdated();
            break;
        case Property::upscaleDlssMode:
            emit settingsUpdated();
            break;
        case Property::upscaleDlssPreset:
            emit settingsUpdated();
            break;
        case Property::upscaleOpticalFlowQuality:
            emit settingsUpdated();
            break;
        case Property::upscaleVsrEnabled:
            emit settingsUpdated();
            break;
        case Property::upscaleVsrQuality:
            emit settingsUpdated();
            break;
        case Property::upscaleSizeMode:
            emit settingsUpdated();
            break;
        case Property::upscaleScaleFactor:
            emit settingsUpdated();
            break;
        case Property::upscaleWidth:
            emit settingsUpdated();
            break;
        case Property::upscaleHeight:
            emit settingsUpdated();
            break;
        case Property::upscaleAspectLock:
            emit settingsUpdated();
            break;
        case Property::upscaleHdrEnabled:
            emit settingsUpdated();
            break;
        case Property::upscaleHdrContrast:
            emit settingsUpdated();
            break;
        case Property::upscaleHdrSaturation:
            emit settingsUpdated();
            break;
        case Property::upscaleHdrMiddleGray:
            emit settingsUpdated();
            break;
        case Property::upscaleHdrPeakLuminance:
            emit settingsUpdated();
            break;
        case Property::upscaleHdrPrecision:
            emit settingsUpdated();
            break;
        case Property::upscaleCodec:
            emit settingsUpdated();
            break;
        case Property::upscaleContainer:
            emit settingsUpdated();
            break;
        case Property::upscaleQuality:
            emit settingsUpdated();
            break;
        case Property::upscaleImageRenameMode:
            emit settingsUpdated();
            break;
        case Property::upscaleImageCustomSuffix:
            emit settingsUpdated();
            break;
        case Property::upscaleRenameMode:
            emit settingsUpdated();
            break;
        case Property::upscaleCustomSuffix:
            emit settingsUpdated();
            break;
        case Property::fiFrameRateMode:
        case Property::fiMultiplier:
        case Property::fiTargetFps:
            emit settingsUpdated();
            break;
        case Property::fiProvider:
            emit settingsUpdated();
            break;
        case Property::fsrFrameRateMode:
        case Property::fsrMultiplier:
        case Property::fsrTargetFps:
            emit settingsUpdated();
            break;
        case Property::fsrAdapterId:
            emit settingsUpdated();
            break;
        case Property::fsrStatus:
            emit runtimeStateChanged();
            break;
        case Property::fiOpticalFlowQuality:
            emit settingsUpdated();
            break;
        case Property::fiCodec:
            emit settingsUpdated();
            break;
        case Property::fiContainer:
            emit settingsUpdated();
            break;
        case Property::fiQuality:
            emit settingsUpdated();
            break;
        case Property::fiPreviewLength:
            emit settingsUpdated();
            break;
        case Property::fiHdrMode:
            emit settingsUpdated();
            break;
        case Property::fiRenameMode:
            emit settingsUpdated();
            break;
        case Property::fiCustomSuffix:
            emit settingsUpdated();
            break;
        case Property::videoHdrSupported:
            emit settingsUpdated();
            break;
        case Property::fiHdrSupported:
            emit settingsUpdated();
            break;
        case Property::upscaleHdrSupported:
            emit settingsUpdated();
            break;
        case Property::aiGpuUuid:
            emit settingsUpdated();
            break;
        case Property::videoGpuUuid:
            emit settingsUpdated();
            break;
        case Property::renderValidationMessage:
            emit estimateChanged();
            break;
        case Property::frameGenerationEstimate:
            emit estimateChanged();
            break;
        case Property::rtxSuperResolutionEstimate:
            emit estimateChanged();
            break;
        case Property::outputEstimate:
            emit estimateChanged();
            break;
        case Property::currentLogPath:
            break;
        case Property::fullLog:
            break;
        }
    }
};
struct SettingBinding {
    Property property;
    const char* key;
};
inline constexpr SettingBinding settingBindings[] = {
    {Property::ffmpegDevice, "ffmpeg_device"},
    {Property::sharpeningMethod, "sharpening_method"},
    {Property::casSharpness, "cas_sharpness"},
    {Property::grainAmount, "grain_amount"},
    {Property::grainSize, "grain_size"},
    {Property::grainColor, "grain_color"},
    {Property::grainResponse, "grain_response"},
    {Property::grainSeed, "grain_seed"},
    {Property::grainAnimated, "grain_animated"},
    {Property::lutFile, "lut_path"},
    {Property::lutResolution, "lut_resolution"},
    {Property::lutReferenceImage, "lut_reference_image"},
    {Property::upscalePreviewLength, "upscale_preview_length"},
    {Property::nrPreviewLength, "nr_preview_length"},
    {Property::nrStyle, "nr_style"},
    {Property::upscalingFactor, "upscaling_factor"},
    {Property::imageScalingFilter, "image_scaling_filter"},
    {Property::videoScalingFilter, "video_scaling_filter"},
    {Property::nrScaleMethod, "nr_scale_method"},
    {Property::nrDlssMode, "nr_dlss_mode"},
    {Property::nrDlssPreset, "nr_dlss_preset"},
    {Property::nrIntensity, "nr_intensity"},
    {Property::nrPasses, "nr_passes"},
    {Property::opticalFlowQuality, "nr_optical_flow_quality"},
    {Property::localToneStrength, "local_tone_strength"},
    {Property::localStructureStrength, "local_structure_strength"},
    {Property::skinStructureStrength, "skin_structure_strength"},
    {Property::automaticMask, "automatic_mask"},
    {Property::imageFormat, "image_format"},
    {Property::imageQuality, "image_quality"},
    {Property::imageBitDepth, "image_bit_depth"},
    {Property::imageRenameMode, "image_rename_mode"},
    {Property::imageCustomSuffix, "image_custom_suffix"},
    {Property::videoCodec, "codec"},
    {Property::videoQuality, "quality"},
    {Property::videoHdrMode, "hdr_mode"},
    {Property::videoRenameMode, "video_rename_mode"},
    {Property::videoCustomSuffix, "video_custom_suffix"},
    {Property::upscaleImageEngine, "upscale_image_engine"},
    {Property::upscaleImageDlssMode, "upscale_image_dlss_mode"},
    {Property::upscaleImageDlssPreset, "upscale_image_dlss_preset"},
    {Property::upscaleImageVsrQuality, "upscale_image_vsr_quality"},
    {Property::upscaleImageSizeMode, "upscale_image_size_mode"},
    {Property::upscaleImageScaleFactor, "upscale_image_scale_factor"},
    {Property::upscaleImageWidth, "upscale_image_width"},
    {Property::upscaleImageHeight, "upscale_image_height"},
    {Property::upscaleImageAspectLock, "upscale_image_aspect_lock"},
    {Property::upscaleImageOutputFormat, "upscale_image_output_format"},
    {Property::upscaleImageQuality, "upscale_image_quality"},
    {Property::upscaleEngine, "upscale_engine"},
    {Property::upscaleDlssMode, "upscale_dlss_mode"},
    {Property::upscaleDlssPreset, "upscale_dlss_preset"},
    {Property::upscaleOpticalFlowQuality, "upscale_optical_flow_quality"},
    {Property::upscaleVsrEnabled, "upscale_vsr_enabled"},
    {Property::upscaleVsrQuality, "upscale_vsr_quality"},
    {Property::upscaleSizeMode, "upscale_size_mode"},
    {Property::upscaleScaleFactor, "upscale_scale_factor"},
    {Property::upscaleWidth, "upscale_width"},
    {Property::upscaleHeight, "upscale_height"},
    {Property::upscaleAspectLock, "upscale_aspect_lock"},
    {Property::upscaleHdrEnabled, "upscale_hdr_enabled"},
    {Property::upscaleHdrContrast, "upscale_hdr_contrast"},
    {Property::upscaleHdrSaturation, "upscale_hdr_saturation"},
    {Property::upscaleHdrMiddleGray, "upscale_hdr_middle_gray"},
    {Property::upscaleHdrPeakLuminance, "upscale_hdr_peak_luminance"},
    {Property::upscaleHdrPrecision, "upscale_hdr_precision"},
    {Property::upscaleCodec, "upscale_codec"},
    {Property::upscaleQuality, "upscale_quality"},
    {Property::upscaleImageRenameMode, "upscale_image_rename_mode"},
    {Property::upscaleImageCustomSuffix, "upscale_image_custom_suffix"},
    {Property::upscaleRenameMode, "upscale_rename_mode"},
    {Property::upscaleCustomSuffix, "upscale_custom_suffix"},
    {Property::fiFrameRateMode, "frame_interpolation_frame_rate_mode"},
    {Property::fiMultiplier, "frame_interpolation_multiplier"},
    {Property::fiTargetFps, "frame_interpolation_target_fps"},
    {Property::fiProvider, "frame_interpolation_provider"},
    {Property::fsrFrameRateMode, "fsr_frame_generation_frame_rate_mode"},
    {Property::fsrMultiplier, "fsr_frame_generation_multiplier"},
    {Property::fsrTargetFps, "fsr_frame_generation_target_fps"},
    {Property::fsrAdapterId, "fsr_adapter_id"},
    {Property::fiOpticalFlowQuality, "frame_interpolation_optical_flow_quality"},
    {Property::fiCodec, "frame_interpolation_codec"},
    {Property::fiQuality, "frame_interpolation_quality"},
    {Property::fiPreviewLength, "frame_interpolation_preview_length"},
    {Property::fiHdrMode, "frame_interpolation_hdr_mode"},
    {Property::fiRenameMode, "frame_interpolation_rename_mode"},
    {Property::fiCustomSuffix, "frame_interpolation_custom_suffix"},
    {Property::aiGpuUuid, "ai_gpu_uuid"},
    {Property::videoGpuUuid, "video_gpu_uuid"},
};
} // namespace ve
