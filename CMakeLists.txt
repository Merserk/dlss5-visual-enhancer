cmake_minimum_required(VERSION 3.28)
project(VisualEnhancer VERSION 18.0.0 LANGUAGES C CXX RC)

if(NOT WIN32 OR NOT MSVC OR NOT CMAKE_CXX_COMPILER_ARCHITECTURE_ID STREQUAL "x64")
    message(FATAL_ERROR "Visual Enhancer requires a Windows MSVC x64 toolchain.")
endif()

get_filename_component(VE_WORKSPACE_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/.." ABSOLUTE)
set(VE_NATIVE_ROOT "${VE_WORKSPACE_ROOT}/native (dev)" CACHE PATH "Native engine source and SDK directory")
set(VE_TEST_ROOT "${VE_WORKSPACE_ROOT}/Tests" CACHE PATH "Permanent test source directory")

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_AUTOMOC ON)
set(CMAKE_AUTORCC ON)
find_package(Qt6 6.12...<6.13 REQUIRED COMPONENTS Core Gui Qml Quick QuickControls2 Multimedia Network Svg Concurrent)
get_target_property(VE_QT_PLATFORM_INCLUDES Qt6::Platform INTERFACE_INCLUDE_DIRECTORIES)
if(NOT VE_QT_PLATFORM_INCLUDES MATCHES "[/\\\\]mkspecs[/\\\\]win32-msvc(;|$)")
    message(FATAL_ERROR "Visual Enhancer requires the Qt 6.12.x MSVC x64 kit.")
endif()

file(GLOB_RECURSE LIBRAW_SOURCES CONFIGURE_DEPENDS "third_party/LibRaw-0.22.2/src/*.cpp")
list(FILTER LIBRAW_SOURCES EXCLUDE REGEX "_ph\\.cpp$")
set(ZLIB_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(ZLIB_BUILD_TESTING OFF CACHE BOOL "" FORCE)
set(ZLIB_BUILD_SHARED OFF CACHE BOOL "" FORCE)
set(ZLIB_INSTALL OFF CACHE BOOL "" FORCE)
add_subdirectory(third_party/zlib-1.3.2 EXCLUDE_FROM_ALL)
set(ENABLE_SHARED OFF CACHE BOOL "" FORCE)
set(ENABLE_STATIC ON CACHE BOOL "" FORCE)
set(WITH_SIMD OFF CACHE BOOL "" FORCE)
set(WITH_TURBOJPEG OFF CACHE BOOL "" FORCE)
include(ExternalProject)
set(VE_JPEG_BINARY "${CMAKE_CURRENT_BINARY_DIR}/jpeg")
file(MAKE_DIRECTORY "${VE_JPEG_BINARY}")
ExternalProject_Add(ve_jpeg_build
    SOURCE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/third_party/libjpeg-turbo-3.1.3"
    BINARY_DIR "${VE_JPEG_BINARY}"
    CMAKE_ARGS -DENABLE_SHARED=OFF -DENABLE_STATIC=ON -DWITH_SIMD=OFF -DWITH_TURBOJPEG=OFF -DWITH_CRT_DLL=ON
    BUILD_COMMAND "${CMAKE_COMMAND}" --build <BINARY_DIR> --config $<CONFIG> --target jpeg-static --parallel 8
    INSTALL_COMMAND ""
)
add_library(ve_jpeg STATIC IMPORTED GLOBAL)
foreach(config Debug Release RelWithDebInfo MinSizeRel)
    string(TOUPPER "${config}" upper)
    set_target_properties(ve_jpeg PROPERTIES "IMPORTED_LOCATION_${upper}" "${VE_JPEG_BINARY}/${config}/jpeg-static.lib")
endforeach()
add_dependencies(ve_jpeg ve_jpeg_build)
add_library(ve_libraw STATIC ${LIBRAW_SOURCES})
target_include_directories(ve_libraw SYSTEM PUBLIC third_party/LibRaw-0.22.2)
target_include_directories(ve_libraw PRIVATE third_party/libjpeg-turbo-3.1.3/src "${VE_JPEG_BINARY}")
target_compile_definitions(ve_libraw PUBLIC LIBRAW_NODLL PRIVATE USE_ZLIB USE_JPEG)
target_link_libraries(ve_libraw PRIVATE ws2_32 zlibstatic ve_jpeg)

add_library(ve_core STATIC
    src/configuration/settings.cpp
    src/infrastructure/paths.cpp
    src/infrastructure/processing_cache.cpp
    src/infrastructure/process.cpp
    src/infrastructure/log.cpp
    src/infrastructure/vulkan_device.cpp
    src/media/media.cpp
    src/media/video_preview.cpp
    src/media/video_backend.cpp
    src/media/image_codecs.cpp
    src/media/png_encoder.cpp
    src/media/image_metadata.cpp
    src/media/image_alpha.cpp
    src/media/packet_stream.cpp
    src/media/cuda_video.cpp
    src/media/d3d_video.cpp
    src/media/d3d11_video.cpp
    src/media/d3d_nvenc.cpp
    src/media/d3d_amf.cpp
    src/media/host_video.cpp
    src/media/video_delivery.cpp
    src/media/frame_batch.cpp
    src/processing/native_engines.cpp
    src/processing/fsr_native.cpp
    src/processing/fsr_temporal.cpp
    src/processing/fsr_video.cpp
    src/processing/fsr_worker.cpp
    src/processing/reshade_native.cpp
    src/processing/reshade_cuda_adapter.cpp
    src/processing/reshade_video.cpp
    src/processing/reshade_worker.cpp
    src/processing/vfx_native.cpp
    src/processing/vfx_video.cpp
    src/processing/vfx_worker.cpp
    src/processing/neural_video_delivery.cpp
    src/processing/effects.cpp
    src/processing/frame_stream.cpp
    src/processing/video_still_preview.cpp
    src/processing/color_analysis.cpp
    src/processing/pipeline.cpp
    src/processing/interpolation_video.cpp
)
target_include_directories(ve_core PUBLIC src "${VE_NATIVE_ROOT}/modules" PRIVATE third_party/libheif/include third_party/ffmpeg)
target_include_directories(ve_core PRIVATE third_party/libjpeg-turbo-3.1.3/src "${VE_JPEG_BINARY}")
target_include_directories(ve_core SYSTEM PRIVATE third_party)
target_include_directories(ve_core SYSTEM PRIVATE "${VE_NATIVE_ROOT}/external/VideoCodecHeaders-12.2")
target_include_directories(ve_core SYSTEM PRIVATE "${VE_NATIVE_ROOT}/external/NVIDIA GPU Computing Toolkit/CUDA/v13.0/include")
target_link_libraries(ve_core PUBLIC Qt6::Core Qt6::Gui Qt6::Network Qt6::Concurrent Qt6::Svg ve_libraw)
target_link_libraries(ve_core PRIVATE zlibstatic ve_jpeg d3d12 d3d11 dxgi dxguid)
target_compile_definitions(ve_core PUBLIC NOMINMAX WIN32_LEAN_AND_MEAN UNICODE _UNICODE
    VE_APP_VERSION="${PROJECT_VERSION}")

add_library(ve_desktop STATIC
    src/application/qml_facade.hpp
    src/application/controller.cpp
    src/application/controller_cache.cpp
    src/application/controller_settings.cpp
    src/application/controller_workflows.cpp
    src/presentation/batch_model.cpp
    src/presentation/image_providers.cpp
    src/presentation/preview_player.cpp
    src/presentation/window_chrome.cpp
)
target_link_libraries(ve_desktop PUBLIC ve_core Qt6::Qml Qt6::Quick Qt6::QuickControls2 Qt6::Multimedia dwmapi shell32 user32 propsys ole32)

function(ve_windows_identity target description filename icon identity)
    set(VE_ICON_PATH "${CMAKE_CURRENT_SOURCE_DIR}/${icon}")
    set(VE_VERSION_NUMERIC "${PROJECT_VERSION_MAJOR},${PROJECT_VERSION_MINOR},${PROJECT_VERSION_PATCH},0")
    set(VE_VERSION_STRING "${PROJECT_VERSION}.0")
    set(VE_PRODUCT_NAME "Visual Enhancer")
    set(VE_FILE_DESCRIPTION "${description}")
    set(VE_ORIGINAL_FILENAME "${filename}")
    set(VE_ASSEMBLY_IDENTITY "${identity}")
    configure_file(src/presentation/application.rc.in "${CMAKE_CURRENT_BINARY_DIR}/${target}.rc" @ONLY)
    configure_file(src/presentation/application.manifest.in "${CMAKE_CURRENT_BINARY_DIR}/${target}.manifest" @ONLY)
    target_sources(${target} PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/${target}.rc")
    target_link_options(${target} PRIVATE /MANIFEST:EMBED
        "/MANIFESTINPUT:${CMAKE_CURRENT_BINARY_DIR}/${target}.manifest")
endfunction()

add_executable(visual_enhancer WIN32 src/application/main.cpp src/infrastructure/runtime_paths.qrc)
set_target_properties(visual_enhancer PROPERTIES OUTPUT_NAME "Visual Enhancer")
target_link_libraries(visual_enhancer PRIVATE ve_desktop)
ve_windows_identity(visual_enhancer "Visual Enhancer" "Visual Enhancer.exe"
    src/desktop/app.ico Merserk.VisualEnhancer)

add_executable(ve_cli src/console/main.cpp src/console/cli.cpp src/infrastructure/runtime_paths.qrc)
set_target_properties(ve_cli PROPERTIES OUTPUT_NAME "VE_CLI")
target_link_libraries(ve_cli PRIVATE ve_core shell32)
ve_windows_identity(ve_cli "Visual Enhancer CLI" "VE_CLI.exe"
    src/console/cmd.ico Merserk.VisualEnhancer.CLI)

if(MSVC)
    foreach(target ve_core ve_desktop visual_enhancer ve_cli)
        target_compile_options(${target} PRIVATE /guard:cf /sdl /GS)
    endforeach()
    foreach(target ve_libraw zlibstatic)
        target_compile_options(${target} PRIVATE /guard:cf /GS)
    endforeach()
    foreach(target ve_core ve_desktop visual_enhancer ve_cli)
        target_compile_options(${target} PRIVATE /W4 /permissive- /utf-8 /Zc:__cplusplus /EHsc)
    endforeach()
    foreach(target visual_enhancer ve_cli)
        target_link_options(${target} PRIVATE /GUARD:CF /DYNAMICBASE /HIGHENTROPYVA /NXCOMPAT /CETCOMPAT)
    endforeach()
endif()

option(BUILD_TESTING "Build the permanent unit and contract tests" OFF)
include(CTest)
if(BUILD_TESTING)
    find_package(Qt6 6.12...<6.13 REQUIRED COMPONENTS Test)
    add_executable(ve_fsr_directx_export_smoke "${VE_TEST_ROOT}/fsr_directx_export_smoke.cpp")
    target_link_libraries(ve_fsr_directx_export_smoke PRIVATE ve_core)
    add_executable(ve_temporal_cadence_tests "${VE_TEST_ROOT}/temporal_cadence_tests.cpp")
    add_executable(ve_fsr_settings_tests "${VE_TEST_ROOT}/fsr_settings_tests.cpp")
    add_executable(ve_frame_generation_settings_tests "${VE_TEST_ROOT}/frame_generation_settings_tests.cpp")
    target_link_libraries(ve_frame_generation_settings_tests PRIVATE ve_core Qt6::Test)
    add_test(NAME frame_generation_settings_contracts COMMAND $<TARGET_FILE:ve_frame_generation_settings_tests> -o -,txt)
    add_executable(ve_fsr_application_smoke "${VE_TEST_ROOT}/fsr_application_smoke.cpp")
    add_executable(ve_fsr_startup_smoke "${VE_TEST_ROOT}/fsr_startup_smoke.cpp")
    target_link_libraries(ve_fsr_startup_smoke PRIVATE ve_core)
    add_executable(ve_frame_generation_preview_smoke "${VE_TEST_ROOT}/frame_generation_preview_smoke.cpp")
    target_link_libraries(ve_frame_generation_preview_smoke PRIVATE ve_core)
    target_link_libraries(ve_fsr_application_smoke PRIVATE ve_core)
    add_executable(ve_fsr_lifecycle_smoke "${VE_TEST_ROOT}/fsr_lifecycle_smoke.cpp")
    target_link_libraries(ve_fsr_lifecycle_smoke PRIVATE ve_core)
    add_executable(ve_fsr_adaptive_smoke "${VE_TEST_ROOT}/fsr_adaptive_smoke.cpp")
    target_link_libraries(ve_fsr_adaptive_smoke PRIVATE ve_core)
    add_library(ve_fsr_bad_abi SHARED "${VE_TEST_ROOT}/fsr_bad_abi.cpp")
    add_executable(ve_fsr_loader_smoke "${VE_TEST_ROOT}/fsr_loader_smoke.cpp")
    target_link_libraries(ve_fsr_loader_smoke PRIVATE ve_core)
    add_dependencies(ve_fsr_loader_smoke ve_fsr_bad_abi)
    target_link_libraries(ve_fsr_settings_tests PRIVATE ve_core Qt6::Test)
    add_test(NAME fsr_settings_contracts COMMAND $<TARGET_FILE:ve_fsr_settings_tests> -o -,txt)
    target_link_libraries(ve_temporal_cadence_tests PRIVATE ve_core Qt6::Test)
    add_test(NAME temporal_cadence_contracts COMMAND $<TARGET_FILE:ve_temporal_cadence_tests> -o -,txt)
    get_filename_component(VE_QT_RUNTIME_ROOT "${Qt6_DIR}/../../.." ABSOLUTE)
    add_executable(ve_storage_tests "${VE_TEST_ROOT}/storage_tests.cpp")
    add_executable(ve_dlss_rr_settings_tests "${VE_TEST_ROOT}/dlss_rr_settings_tests.cpp")
    add_executable(ve_reshade_settings_tests "${VE_TEST_ROOT}/reshade_settings_tests.cpp")
    target_link_libraries(ve_reshade_settings_tests PRIVATE ve_core Qt6::Test)
    add_test(NAME reshade_settings_contracts COMMAND $<TARGET_FILE:ve_reshade_settings_tests> -o -,txt)
    set_tests_properties(reshade_settings_contracts PROPERTIES
        WORKING_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}"
        ENVIRONMENT "VE_APP_ROOT=${VE_WORKSPACE_ROOT}/Visual Enhancer"
        ENVIRONMENT_MODIFICATION "PATH=path_list_prepend:${VE_QT_RUNTIME_ROOT}/bin"
        TIMEOUT 45)
    add_executable(ve_dlss_rr_delivery_smoke "${VE_TEST_ROOT}/dlss_rr_delivery_smoke.cpp")
    target_link_libraries(ve_dlss_rr_delivery_smoke PRIVATE ve_core Qt6::Test)
    add_executable(ve_dlss_rr_initialization_smoke "${VE_TEST_ROOT}/dlss_rr_initialization_smoke.cpp")
    target_link_libraries(ve_dlss_rr_initialization_smoke PRIVATE ve_core Qt6::Test)
    add_executable(ve_dlss_rr_mixed_preview_smoke "${VE_TEST_ROOT}/dlss_rr_mixed_preview_smoke.cpp")
    target_link_libraries(ve_dlss_rr_mixed_preview_smoke PRIVATE ve_core Qt6::Test)
    add_executable(ve_dlss_rr_parked_preview_smoke "${VE_TEST_ROOT}/dlss_rr_parked_preview_smoke.cpp")
    target_link_libraries(ve_dlss_rr_parked_preview_smoke PRIVATE ve_core Qt6::Test)
    add_executable(ve_dlss_rr_stream_timing_smoke "${VE_TEST_ROOT}/dlss_rr_stream_timing_smoke.cpp")
    target_link_libraries(ve_dlss_rr_stream_timing_smoke PRIVATE ve_core Qt6::Test)
    add_executable(ve_dlss_rr_worker_preview_smoke "${VE_TEST_ROOT}/dlss_rr_worker_preview_smoke.cpp")
    target_link_libraries(ve_dlss_rr_worker_preview_smoke PRIVATE ve_core Qt6::Test)
    target_link_libraries(ve_dlss_rr_settings_tests PRIVATE ve_core Qt6::Test)
    add_test(NAME dlss_rr_settings_contracts COMMAND $<TARGET_FILE:ve_dlss_rr_settings_tests> -o -,txt)
    target_link_libraries(ve_storage_tests PRIVATE ve_desktop Qt6::Test)
    add_executable(ve_processing_cache_tests "${VE_TEST_ROOT}/processing_cache_tests.cpp")
    target_link_libraries(ve_processing_cache_tests PRIVATE ve_desktop Qt6::Test)
    add_executable(ve_resolution_tests "${VE_TEST_ROOT}/resolution_tests.cpp")
    target_link_libraries(ve_resolution_tests PRIVATE ve_desktop Qt6::Test)
    add_executable(ve_translation_tests "${VE_TEST_ROOT}/translation_tests.cpp")
    target_link_libraries(ve_translation_tests PRIVATE ve_desktop Qt6::Test)
    target_compile_options(ve_translation_tests PRIVATE /utf-8)
    target_compile_definitions(ve_translation_tests PRIVATE
        VE_SOURCE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/src"
        VE_TRANSLATION_DIR="${CMAKE_CURRENT_SOURCE_DIR}/src/desktop/translations"
        VE_LUPDATE="${VE_QT_RUNTIME_ROOT}/bin/lupdate.exe")
    add_executable(ve_desktop_smoke "${VE_TEST_ROOT}/desktop_smoke.cpp")
    target_link_libraries(ve_desktop_smoke PRIVATE ve_desktop Qt6::Test)
    ve_windows_identity(ve_desktop_smoke "Visual Enhancer Desktop Smoke" "ve_desktop_smoke.exe"
        src/desktop/app.ico Merserk.VisualEnhancer.Tests)
    add_executable(ve_vfx_storage_smoke "${VE_TEST_ROOT}/vfx_storage_smoke.cpp")
    target_link_libraries(ve_vfx_storage_smoke PRIVATE ve_desktop)
    add_executable(ve_media_resource_smoke "${VE_TEST_ROOT}/media_resource_smoke.cpp")
    target_link_libraries(ve_media_resource_smoke PRIVATE ve_desktop Qt6::Test psapi)
    add_executable(ve_desktop_memory_smoke "${VE_TEST_ROOT}/desktop_memory_smoke.cpp")
    target_link_libraries(ve_desktop_memory_smoke PRIVATE ve_desktop Qt6::Test psapi)
    add_executable(ve_process_stream_tests "${VE_TEST_ROOT}/process_stream_tests.cpp")
    target_link_libraries(ve_process_stream_tests PRIVATE ve_core Qt6::Test psapi)
    add_test(NAME process_stream_contracts COMMAND $<TARGET_FILE:ve_process_stream_tests> -o -,txt)
    set_tests_properties(process_stream_contracts PROPERTIES
        ENVIRONMENT_MODIFICATION "PATH=path_list_prepend:${VE_QT_RUNTIME_ROOT}/bin"
        TIMEOUT 45)
    add_executable(ve_video_metadata_tests "${VE_TEST_ROOT}/video_metadata_tests.cpp")
    target_link_libraries(ve_video_metadata_tests PRIVATE ve_core Qt6::Test)
    target_compile_options(ve_video_metadata_tests PRIVATE /utf-8)
    add_test(NAME video_metadata_contracts COMMAND $<TARGET_FILE:ve_video_metadata_tests> -o -,txt)
    set_tests_properties(video_metadata_contracts PROPERTIES
        ENVIRONMENT "VE_APP_ROOT=${VE_WORKSPACE_ROOT}/Visual Enhancer"
        ENVIRONMENT_MODIFICATION "PATH=path_list_prepend:${VE_QT_RUNTIME_ROOT}/bin"
        TIMEOUT 60)
    add_executable(ve_cli_cancel_probe "${VE_TEST_ROOT}/cli_cancel_probe.cpp")
    set_target_properties(ve_cli_cancel_probe PROPERTIES AUTOMOC OFF AUTORCC OFF)
    target_compile_definitions(ve_cli_cancel_probe PRIVATE NOMINMAX WIN32_LEAN_AND_MEAN UNICODE _UNICODE)
    target_link_libraries(ve_cli_cancel_probe PRIVATE user32)
    add_test(NAME storage_contracts COMMAND $<TARGET_FILE:ve_storage_tests> -o -,txt)
    add_test(NAME processing_cache_contracts COMMAND $<TARGET_FILE:ve_processing_cache_tests> -o -,txt)
    add_test(NAME resolution_contracts COMMAND $<TARGET_FILE:ve_resolution_tests> -o -,txt)
    add_test(NAME translation_contracts COMMAND $<TARGET_FILE:ve_translation_tests> -o -,txt)
    set_tests_properties(storage_contracts processing_cache_contracts translation_contracts resolution_contracts temporal_cadence_contracts fsr_settings_contracts frame_generation_settings_contracts PROPERTIES
        WORKING_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}"
        ENVIRONMENT "QT_QPA_PLATFORM=offscreen;QT_PLUGIN_PATH=${VE_QT_RUNTIME_ROOT}/plugins"
        ENVIRONMENT_MODIFICATION "PATH=path_list_prepend:${VE_QT_RUNTIME_ROOT}/bin"
        TIMEOUT 60)
    if(EXISTS "${VE_TEST_ROOT}/native_tests.cpp")
        add_executable(ve_tests "${VE_TEST_ROOT}/native_tests.cpp")
        target_link_libraries(ve_tests PRIVATE ve_desktop Qt6::Test)
        target_compile_definitions(ve_tests PRIVATE VE_TEST_VIDEO_PREVIEW_CACHE)
        add_test(NAME native_contracts COMMAND $<TARGET_FILE:ve_tests> -o -,txt)
        add_test(NAME windows_chrome COMMAND $<TARGET_FILE:ve_tests> nativeMaximizeAndRestore -o -,txt)
        set_tests_properties(windows_chrome PROPERTIES
            WORKING_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}"
            ENVIRONMENT "QT_QPA_PLATFORM=windows;QT_PLUGIN_PATH=${VE_QT_RUNTIME_ROOT}/plugins"
            ENVIRONMENT_MODIFICATION "PATH=path_list_prepend:${VE_QT_RUNTIME_ROOT}/bin"
            TIMEOUT 45)
        set_tests_properties(native_contracts PROPERTIES
            WORKING_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}"
            ENVIRONMENT "QT_QPA_PLATFORM=offscreen;QT_PLUGIN_PATH=${VE_QT_RUNTIME_ROOT}/plugins"
            ENVIRONMENT_MODIFICATION "PATH=path_list_prepend:${VE_QT_RUNTIME_ROOT}/bin"
            TIMEOUT 60)
    else()
        message(WARNING "Legacy native/window test sources are absent; storage contracts remain enabled.")
    endif()
endif()

# tools/deploy.ps1 packages only the runnable application and its resources.
