"""Per-part encoding and GPU-free incremental packet delivery."""
from __future__ import annotations

from fractions import Fraction
import json
from pathlib import Path

import av

from ..core import ffmpeg
from ..core.disk_paths import OutputFile
from ..core.ffmpeg.audio import plan_audio_streams
from ..core.jobs import capture_process, prepare_job
from ..core.paths import FFMPEG


class SegmentEncoder:
    def __init__(self, path, state, settings, ordinal, *, cuda=False, controller=None):
        from ..frame_interpolation.processor import _set_color_properties
        self.path, self.state, self.settings = Path(path), state, settings
        self.count = 0
        self.packet_times = []
        self.transfer_pool = None
        self.reformatter = av.video.reformatter.VideoReformatter()
        self.timings_path = Path(str(path) + ".json")
        codec = ffmpeg._normalize_codec(settings.codec)
        segment_format = "mov" if codec.startswith("ProRes") else "nut"
        self.container = av.open(str(path), mode="w", format=segment_format)
        self.stream = None
        try:
            codec = ffmpeg._normalize_codec(settings.codec)
            names = {"H.264 (NVIDIA NVENC)": "h264_nvenc", "H.265 (NVIDIA NVENC)": "hevc_nvenc",
                     "AV1 (NVIDIA NVENC)": "av1_nvenc", "H.264": "libx264", "H.265": "libx265",
                     "HEVC": "libx265", "AV1": "libsvtav1", "FFV1 Lossless RGB 10-bit": "ffv1",
                     "ProRes Proxy": "prores_ks", "ProRes HQ": "prores_ks"}
            name = names[codec]
            ai_ordinal = ordinal
            if name.endswith("_nvenc") and settings.video_gpu_uuid != "auto":
                from ..core.gpu_detection import detect_gpus
                selected = ffmpeg.resolve_video_gpu(detect_gpus(), settings.video_gpu_uuid,
                    settings.codec, state.width, state.height)
                ordinal = int(selected["cuda_ordinal"])
            if cuda and ordinal != ai_ordinal:
                from ..upscale.video.cuda_transfer import CudaTransferPool
                self.transfer_pool = CudaTransferPool(ai_ordinal, ordinal, controller)
            device = None
            if cuda:
                from av.codec.hwaccel import HWAccel
                device = HWAccel("cuda", device=str(ordinal), options={"primary_ctx": "1"}, is_hw_owned=True)
            self.stream = self.container.add_stream(name, rate=state.rate, **({"hwaccel": device} if device else {}))
            self.stream.width, self.stream.height = state.width, state.height
            depth = ffmpeg.output_video_depth(state.depth, settings.codec, state.hdr)
            if name == "ffv1":
                pixel_format = "gbrp10le"
            elif name == "prores_ks":
                pixel_format = "yuv422p10le"
            elif name == "h264_nvenc" or name == "libx264":
                pixel_format = "nv12" if cuda else "yuv420p"
            else:
                pixel_format = ("p010le" if name.endswith("_nvenc") else "yuv420p10le") if depth > 8 else ("nv12" if cuda else "yuv420p")
            self.pixel_format = pixel_format
            self.stream.pix_fmt = "cuda" if cuda else pixel_format
            if cuda:
                self.stream.codec_context.sw_format = pixel_format
            # A fine common time base preserves fractional and VFR timestamps.
            self.time_base = Fraction(1, 60_000_000)
            self.stream.time_base = self.stream.codec_context.time_base = self.time_base
            quality = ffmpeg.resolve_encoding_quality(settings.quality, settings.codec,
                state.width, state.height, float(state.rate), hdr_mode=state.hdr)
            values = {}
            if name.endswith("_nvenc"):
                values = {"preset": "p6", "rc": "vbr", "gpu": str(ordinal), "forced-idr": "1"}
                if name != "av1_nvenc":
                    values["tune"] = "hq"
                if name == "h264_nvenc":
                    values["profile"] = "high"
                values.update(ffmpeg.nvenc_rate_control_options(quality))
                if quality["mode"] == "constant-quality":
                    self.stream.codec_context.bit_rate = 0
                else:
                    self.stream.codec_context.bit_rate = int(quality["target_bitrate_kbps"]) * 1000
            elif name in {"libx264", "libx265", "libsvtav1"}:
                values["preset"] = "6" if name == "libsvtav1" else "slow"
                if name == "libx265":
                    values["x265-params"] = "open-gop=0"
                if quality["mode"] == "constant-quality":
                    values.update(ffmpeg.software_max_quality_options(name))
                    self.stream.codec_context.bit_rate = 0
                else:
                    self.stream.codec_context.bit_rate = int(quality["target_bitrate_kbps"]) * 1000
            elif name == "ffv1":
                values = {"level": "3", "slicecrc": "1"}
            elif name == "prores_ks":
                values = {"profile": "0" if codec == "ProRes Proxy" else "3"}
                if quality.get("bits_per_mb") is not None:
                    values["bits_per_mb"] = str(int(quality["bits_per_mb"]))
            self.stream.codec_context.options = values
            colors = dict(state.metadata)
            if name != "ffv1":
                colors.update(color_space="bt2020nc" if state.hdr else "bt709", color_range="tv")
            _set_color_properties(self.stream.codec_context, colors, state.hdr)
        except BaseException:
            self.close(abort=True)
            raise

    def write(self, frame, stamp, start, duration=None):
        if frame.format.name == "cuda" and self.transfer_pool:
            frame, _ = self.transfer_pool.transfer(frame)
        if frame.format.name != "cuda":
            from av.video.reformatter import Colorspace, ColorRange
            destination_space = Colorspace.BT2020 if self.state.hdr else Colorspace.ITU709
            source_space = {1: Colorspace.ITU709, 4: Colorspace.FCC, 5: Colorspace.ITU601,
                            6: Colorspace.ITU601, 7: Colorspace.SMPTE240M, 9: Colorspace.BT2020}.get(
                                frame.colorspace, destination_space)
            source_range = ColorRange.JPEG if frame.format.is_rgb else {
                1: ColorRange.MPEG, 2: ColorRange.JPEG}.get(frame.color_range, ColorRange.MPEG)
            frame = self.reformatter.reformat(frame, width=self.state.width, height=self.state.height,
                format=self.pixel_format, src_colorspace=source_space, dst_colorspace=destination_space,
                src_color_range=source_range,
                dst_color_range=ColorRange.JPEG if self.pixel_format.startswith("gbr") else ColorRange.MPEG)
            # Tag after conversion. Explicit destination transfer/primaries in
            # bundled swscale also transform pixels, changing existing PQ/RGB.
            for name in ("colorspace", "color_range", "color_trc", "color_primaries"):
                setattr(frame, name, getattr(self.stream.codec_context, name))
        frame.pts = round((Fraction(stamp) - Fraction(start)) / self.time_base)
        frame.time_base = self.time_base
        if duration is not None:
            frame.duration = max(1, round(Fraction(duration) / self.time_base))
        for packet in self.stream.encode(frame):
            self._mux(packet)
        self.count += 1

    def _mux(self, packet):
        base = Fraction(packet.time_base)
        self.packet_times.append([str(packet.pts * base) if packet.pts is not None else None,
                                  str(packet.dts * base) if packet.dts is not None else None,
                                  str(packet.duration * base)])
        self.container.mux(packet)

    def close(self, *, abort=False):
        if self.container:
            try:
                if not abort:
                    for packet in self.stream.encode(None):
                        self._mux(packet)
                    self.timings_path.write_text(json.dumps(self.packet_times), encoding="utf-8")
            finally:
                try:
                    self.container.close()
                finally:
                    self.container = self.stream = None
                    if self.transfer_pool:
                        try:
                            self.transfer_pool.close(abort=abort)
                        finally:
                            self.transfer_pool = None


class PacketDelivery:
    """Append each committed part and remove its segment before the next load."""

    def __init__(self, directory, source, destination, settings, controller):
        codec = ffmpeg._normalize_codec(settings.codec)
        self.format = "nut" if codec == "FFV1 Lossless RGB 10-bit" else "mov" if codec.startswith("ProRes") else "mp4"
        self.path = Path(directory) / ("committed-delivery." + self.format)
        self.source, self.destination = Path(source), Path(destination)
        self.settings, self.controller = settings, controller
        self.container = self.stream = None
        self.frames = 0
        self.last_dts = None
        self.extradata = None
        self.codec_name = None
        self.source_time_origin = None

    def append(self, segment, start):
        self.controller.check()
        timings_path = Path(str(segment) + ".json")
        timings = json.loads(timings_path.read_text(encoding="utf-8"))
        with av.open(str(segment)) as part:
            stream = part.streams.video[0]
            if self.container is None:
                options = {"avoid_negative_ts": "disabled"}
                if self.format in {"mp4", "mov"}:
                    options["video_track_timescale"] = "60000000"
                # MP4 retains explicit decode timestamps for reordered codecs.
                # NUT's parser can rederive different DTS from fine VFR clocks.
                self.container = av.open(str(self.path), "w", format=self.format, options=options)
                self.stream = self.container.add_stream_from_template(stream, opaque=True)
                self.extradata = stream.codec_context.extradata
                self.codec_name = stream.codec_context.name
            elif (stream.codec_context.name != self.codec_name or
                  stream.codec_context.extradata != self.extradata):
                raise RuntimeError("Encoded part headers changed; delivery cannot be appended safely.")
            first = True
            count = 0
            for packet in part.demux(stream):
                if not packet.size:
                    continue
                # A CPU-only append is a small commit transaction. Pause is
                # honored immediately after it, avoiding a partial replay.
                if self.controller.cancel.is_set():
                    from ..core.jobs import Cancelled
                    raise Cancelled("Render stopped by user.")
                if first and not packet.is_keyframe:
                    raise RuntimeError("An encoded part must begin with an independent keyframe.")
                first = False
                if count >= len(timings):
                    raise RuntimeError("Encoded part packet timing records are incomplete.")
                pts, dts, packet_duration = timings[count]
                packet.pts = round(Fraction(pts) / packet.time_base) if pts is not None else None
                packet.dts = round(Fraction(dts) / packet.time_base) if dts is not None else None
                packet.duration = round(Fraction(packet_duration) / packet.time_base)
                count += 1
                offset = round(Fraction(start) / packet.time_base)
                if packet.pts is not None:
                    packet.pts += offset
                if packet.dts is not None:
                    packet.dts += offset
                    stamp = Fraction(packet.dts) * packet.time_base
                    if self.last_dts is not None and stamp <= self.last_dts:
                        raise RuntimeError("Encoded part DTS overlaps a committed part.")
                    self.last_dts = stamp
                packet.stream = self.stream
                try:
                    self.container.mux(packet)
                except av.error.FFmpegError as exc:
                    raise RuntimeError(f"Part packet mux failed: pts={packet.pts} dts={packet.dts} "
                                       f"duration={packet.duration} time_base={packet.time_base} "
                                       f"stream_time_base={self.stream.time_base} offset={start}") from exc
                self.frames += 1
            if count != len(timings):
                raise RuntimeError("Encoded part packet accounting differs from its timing records.")
        Path(segment).unlink()
        timings_path.unlink()

    def finish(self, state, duration):
        from ..core.jobs import Paused
        if self.container:
            self.container.close()
            self.container = None
        if not self.frames:
            raise RuntimeError("The pipeline produced no delivery frames.")
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        audio = prepare_job(self.controller, lambda: plan_audio_streams(self.source,
            ffmpeg.resolve_container(self.settings.codec, self.settings.container), self.controller))
        self.controller.report_phase(state="delivery", cache_bytes=0, worker_processes=0, fps=0.0)
        with_output = OutputFile(self.destination)
        chapter_file = None
        try:
            if self.source_time_origin is not None:
                # FFmpeg's implicit chapter shift also uses the input's start
                # time, which can differ from the first video frame (audio
                # priming/offset). Normalize chapter clocks explicitly.
                chapter_file = self.path.with_name("delivery-chapters.ffmetadata")
                lines = [";FFMETADATA1"]
                origin = Fraction(self.source_time_origin)
                def escape(value):
                    return str(value).replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;").replace("#", "\\#").replace("\n", "\\\n")
                with av.open(str(self.source)) as media:
                    for chapter in media.chapters():
                        start = max(Fraction(0), chapter["start"] * chapter["time_base"] - origin)
                        end = min(Fraction(duration), chapter["end"] * chapter["time_base"] - origin)
                        if end <= start:
                            continue
                        lines.extend(["[CHAPTER]", "TIMEBASE=1/60000000", f"START={round(start * 60000000)}",
                                      f"END={round(end * 60000000)}"])
                        lines.extend(f"{escape(key)}={escape(value)}" for key, value in chapter["metadata"].items())
                chapter_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            command = [str(FFMPEG), "-v", "error", "-xerror", "-y",
                       *(["-copyts"] if self.source_time_origin is not None else []), "-i", str(self.path),
                       *(["-itsoffset", f"{-float(self.source_time_origin):.12f}"]
                         if self.source_time_origin is not None else []),
                       "-i", str(self.source),
                       *(["-f", "ffmetadata", "-i", str(chapter_file)] if chapter_file else []),
                       "-map", "0:v:0", "-map", "1:a?", "-map_metadata", "1",
                       "-map_chapters", "2" if chapter_file else "1", "-c:v", "copy", *audio.encoder_args(),
                       "-t", f"{float(duration):.12f}", "-fps_mode", "passthrough",
                       "-avoid_negative_ts", "disabled", "-metadata:s:v:0", "rotate=0", str(with_output.temporary)]
            # Finalization owns no model, decoder or hardware encoder.
            while True:
                try:
                    self.controller.check()
                    result = capture_process(command, timeout=600, controller=self.controller)
                    self.controller.check()
                    if result.returncode:
                        raise RuntimeError("Delivery mux failed: " + result.stderr.decode("utf-8", "replace"))
                    break
                except Paused:
                    self.controller.release_processes()
                    self.controller.wait_for_resume()
                    self.controller.report_phase(state="delivery", cache_bytes=0, worker_processes=0, fps=0.0)
            saved = ffmpeg.probe_video(with_output.temporary, count_mode="packets", controller=self.controller)
            if (int(saved["frames"]) != self.frames or
                    (int(saved["width"]), int(saved["height"])) != (state.width, state.height)):
                raise RuntimeError("Delivery frame count or dimensions differ from committed parts.")
            if state.hdr and (not saved["hdr"] or int(saved["depth"]) < 10):
                raise RuntimeError("Delivery lost HDR signaling or precision.")
            self.controller.check()
            with_output.publish()
        finally:
            with_output.cleanup()
            if chapter_file:
                chapter_file.unlink(missing_ok=True)

    def close(self):
        if self.container:
            self.container.close()
            self.container = None
        self.path.unlink(missing_ok=True)
