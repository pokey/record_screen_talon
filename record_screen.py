import json
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml
from talon import (
    Context,
    Module,
    actions,
    app,
    cron,
    scope,
    screen,
    settings,
    speech_system,
    ui,
)
from talon.canvas import Canvas
from talon.ui import UIErr

CALIBRATION_DISPLAY_BACKGROUND_COLOR = "#1b0026"
CALIBRATION_DISPLAY_DURATION = "50ms"

mod = Module()
mod.tag(
    "recording_screen",
    "tag to to indicate that screen is currently being recorded",
)


screenshot_time_stamp_only = mod.setting(
    "screenshot_time_stamp_only",
    type=bool,
    default=True,
    desc="If `True`, don't actually take a screenshot during recording just capture the timestamp so that we can extract it from the video later",
)

ctx = Context()

sleeping_recording_screen_ctx = Context()
sleeping_recording_screen_ctx.matches = r"""
mode: sleep
and tag: user.recording_screen
"""

recording_screen_ctx = Context()
recording_screen_ctx.matches = r"""
tag: user.recording_screen
"""

recording_screen_vscode_ctx = Context()
recording_screen_vscode_ctx.matches = r"""
tag: user.recording_screen
app: vscode
"""


GIT = "/usr/local/bin/git"

recording_start_time: float
recorders: list[str]
is_recording_face: bool
recording_log_directory: Path
screenshots_directory: Path
snapshots_directory: Path
recording_log_file: Path
recordings_root_dir = Path.home() / "talon-recording-logs"


def log_object(output_object):
    with open(recording_log_file, "a") as out:
        out.write(json.dumps(output_object) + "\n")


def git(*args: str, cwd: Path):
    return subprocess.run(
        [GIT, *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    ).stdout.strip()


@mod.action_class
class Actions:
    def start_recording(
        recorder_1: Optional[str] = None,
        recorder_2: Optional[str] = None,
        recorder_3: Optional[str] = None,
    ):
        """Start recording a talon session"""
        global recording_start_time
        global recording_log_directory
        global screenshots_directory
        global snapshots_directory
        global recording_log_file
        global current_phrase_id
        global recorders

        recorders = list(filter(None, [recorder_1, recorder_2, recorder_3]))

        for recorder in recorders:
            getattr(actions.user, f"{recorder}_check_can_start")()

        recording_log_directory = recordings_root_dir / time.strftime(
            "%Y-%m-%dT%H-%M-%S"
        )
        recording_log_directory.mkdir(parents=True)
        recording_log_file = recording_log_directory / "talon-log.jsonl"

        commands_directory = recording_log_directory / "commands"
        commands_directory.mkdir(parents=True)

        snapshots_directory = recording_log_directory / "snapshots"
        snapshots_directory.mkdir(parents=True)

        screenshots_directory = recording_log_directory / "screenshots"
        screenshots_directory.mkdir(parents=True)

        check_and_log_talon_subdirs()

        ctx.tags = ["user.recording_screen"]

        current_phrase_id = None

        for recorder in recorders:
            actions.sleep("250ms")
            getattr(actions.user, f"{recorder}_start_recording")()

        # Flash a rectangle so that we can synchronize the recording start time
        flash_rect()

        recording_start_time = time.perf_counter()
        start_timestamp_iso = datetime.utcnow().isoformat()

        user_dir: Path = Path(actions.path.talon_user())

        log_object(
            {
                "type": "initialInfo",
                "version": 1,
                "extensionRecordStartPayload": extension_payload,
                "startTimestampISO": start_timestamp_iso,
                "talonDir": str(user_dir.parent),
            }
        )

    def record_screen_stop():
        """Stop recording screen"""
        for recorder in recorders:
            getattr(actions.user, f"{recorder}_check_can_stop")()

        ctx.tags = []

        for recorder in recorders:
            actions.sleep("250ms")
            getattr(actions.user, f"{recorder}_stop_recording")()

        actions.user.post_record_screen_stop_hook()

    def maybe_capture_phrase(j: Any):
        """Possibly capture a phrase; does nothing unless screen recording is active"""

    def maybe_capture_post_phrase(j: Any):
        """Possibly capture a phrase; does nothing unless screen recording is active"""

    def take_snapshot(path: str, metadata: Any, decorated_marks: list[dict]):
        """Take a snapshot of the current app state"""

    def post_record_screen_start_hook():
        """Hook to run after starting a screen recording"""
        pass

    def post_record_screen_stop_hook():
        """Hook to run after stopping a screen recording"""
        pass


def check_and_log_talon_subdirs():
    for directory in Path(actions.path.talon_user()).iterdir():
        if not directory.is_dir():
            continue

        repo_remote_url = git("config", "--get", "remote.origin.url", cwd=directory)

        if not repo_remote_url:
            continue

        if git("status", "--porcelain", cwd=directory):
            app.notify("ERROR: Please commit all git changes")
            raise Exception("Please commit all git changes")

        commit_sha = git("rev-parse", "HEAD", cwd=directory)

        # Represents the path of the given folder within the git repo in
        # which it is contained. This occurs when we sim link a subdirectory
        # of a repository into our talon user directory such as we do with
        # cursorless talon.
        repo_prefix = git("rev-parse", "--show-prefix", cwd=directory)

        log_object(
            {
                "type": "directoryInfo",
                "localPath": str(directory),
                "localRealPath": str(directory.resolve(strict=True)),
                "repoRemoteUrl": repo_remote_url,
                "repoPrefix": repo_prefix,
                "commitSha": commit_sha,
            }
        )


@sleeping_recording_screen_ctx.action_class("user")
class SleepUserActions:
    def maybe_show_history():
        pass


@ctx.action_class("user")
class UserActions:
    def post_record_screen_start_hook():
        pass

    def post_record_screen_stop_hook():
        pass

    def maybe_capture_phrase(j: Any):
        # Turn this one off globally
        pass

    def maybe_capture_post_phrase(j: Any):
        # Turn this one off globally
        pass

    def take_snapshot(path: str, metadata: Any, decorated_marks: list[dict]):
        # Turn this one off globally
        pass


def json_safe(arg: Any):
    """
    Checks whether arg can be json serialized and if so just returns arg as is
    otherwise returns none

    """
    try:
        json.dumps(arg)
        return arg
    except:
        return None


current_phrase_id: Optional[str] = None


@recording_screen_ctx.action_class("user")
class UserActions:
    def maybe_capture_phrase(j: Any):
        global current_phrase_id

        pre_phrase_start = time.perf_counter() - recording_start_time

        words = j.get("text")

        text = actions.user.history_transform_phrase_text(words)

        word_infos = [
            {
                "start": (
                    words[idx].start - recording_start_time
                    if words[idx].start is not None
                    else None
                ),
                "end": (
                    words[idx].end - recording_start_time
                    if words[idx].end is not None
                    else None
                ),
                "text": str(words[idx]),
            }
            for idx in range(len(words))
        ]

        if text is None:
            try:
                speech_start = j["_ts"] - recording_start_time
            except KeyError:
                speech_start = None

            log_object(
                {
                    "type": "talonIgnoredPhrase",
                    "id": str(uuid.uuid4()),
                    "raw_words": word_infos,
                    "timeOffsets": {
                        "speechStart": speech_start,
                        "prePhraseCallbackStart": pre_phrase_start,
                    },
                    "speechTimeout": settings.get("speech.timeout"),
                }
            )

            current_phrase_id = None

            return

        sim = None
        commands = None
        try:
            sim = speech_system._sim(text)
            commands = actions.user.parse_sim(sim)
        except Exception as e:
            app.notify(f'Couldn\'t sim for "{text}"', f"{e}")

        parsed = j["parsed"]

        if commands is not None:
            for idx, capture_list in enumerate(parsed):
                commands[idx]["captures"] = [
                    json_safe(capture) for capture in capture_list
                ]

        current_phrase_id = str(uuid.uuid4())

        decorated_marks = list(extract_decorated_marks(parsed))

        actions.user.take_snapshot(
            str(snapshots_directory / f"{current_phrase_id}-prePhrase.yaml"),
            {"phraseId": current_phrase_id, "type": "prePhrase"},
            decorated_marks,
        )

        pre_command_screenshot = capture_screen(
            screenshots_directory, recording_start_time
        )
        mark_screenshots = take_mark_screenshots(
            decorated_marks, screenshots_directory, recording_start_time
        )

        log_object(
            {
                "type": "talonCommandPhrase",
                "id": current_phrase_id,
                "timeOffsets": {
                    "speechStart": j["_ts"] - recording_start_time,
                    "prePhraseCallbackStart": pre_phrase_start,
                    "prePhraseCallbackEnd": time.perf_counter() - recording_start_time,
                },
                "speechTimeout": settings.get("speech.timeout"),
                "phrase": text,
                "raw_words": word_infos,
                "rawSim": sim,
                "commands": commands,
                "modes": list(scope.get("mode")),
                "tags": list(scope.get("tag")),
                "screenshots": {
                    "decoratedMarks": mark_screenshots,
                    "preCommand": pre_command_screenshot,
                },
            }
        )

    def maybe_capture_post_phrase(j: Any):
        global current_phrase_id

        if current_phrase_id is not None:
            actions.user.take_snapshot(
                str(snapshots_directory / f"{current_phrase_id}-postPhrase.yaml"),
                {"phraseId": current_phrase_id, "type": "postPhrase"},
                [],
            )
            post_phrase_start = time.perf_counter() - recording_start_time
            post_command_screenshot = capture_screen(
                screenshots_directory, recording_start_time
            )

            # NB: This object will get merged with the pre-phrase object during
            # postprocessing.  See
            # https://github.com/pokey/voice_vid/blob/079558a2246875fd651bdd7f5d7b76974dc9b3eb/voice_vid/io/parse_transcript.py#L112-L117
            log_object(
                {
                    "id": current_phrase_id,
                    "commandCompleted": True,
                    "timeOffsets": {
                        "postPhraseCallbackStart": post_phrase_start,
                        "postPhraseCallbackEnd": (
                            time.perf_counter() - recording_start_time
                        ),
                    },
                    "screenshots": {
                        "postCommand": post_command_screenshot,
                    },
                }
            )

            current_phrase_id = None


@recording_screen_vscode_ctx.action_class("user")
class UserActions:
    def take_snapshot(path: str, metadata: Any, decorated_marks: list[dict]):
        try:
            use_pre_phrase_snapshot = actions.user.did_emit_pre_phrase_signal()
        except KeyError:
            use_pre_phrase_snapshot = False

        try:
            ui.active_window().children.find_one(AXRole="AXMenu", max_depth=0)
            menu_showing = True
        except UIErr:
            menu_showing = False
        if not menu_showing:
            try:
                actions.user.vscode_with_plugin_and_wait(
                    "cursorless.takeSnapshot",
                    path,
                    metadata,
                    decorated_marks,
                    use_pre_phrase_snapshot,
                )
            except Exception as e:
                with open(path, "w") as f:
                    yaml.dump({"metadata": metadata, "error": str(e)}, f)
        else:
            with open(path, "w") as f:
                yaml.dump({"metadata": metadata, "isMenuShowing": True}, f)


def flash_rect():
    rect = screen.main_screen().rect

    def on_draw(c):
        c.paint.style = c.paint.Style.FILL
        c.paint.color = CALIBRATION_DISPLAY_BACKGROUND_COLOR
        c.draw_rect(rect)
        cron.after(CALIBRATION_DISPLAY_DURATION, canvas.close)

    canvas = Canvas.from_rect(rect)
    canvas.register("draw", on_draw)
    canvas.freeze()


def take_mark_screenshots(
    decorated_marks: list[dict],
    screenshots_directory: Path,
    recording_start_time: float,
):
    if not decorated_marks:
        return None

    all_decorated_marks_target = {
        "type": "list",
        "elements": [{"type": "primitive", "mark": mark} for mark in decorated_marks],
    }

    with cursorless_recording_paused():
        actions.user.cursorless_single_target_command(
            "highlight", all_decorated_marks_target, "highlight1"
        )

        actions.sleep("50ms")

        all_decorated_marks_screenshot = capture_screen(
            screenshots_directory, recording_start_time
        )

        actions.user.cursorless_single_target_command(
            "highlight",
            {
                "type": "primitive",
                "mark": {"type": "nothing"},
            },
            "highlight1",
        )

    return {"all": all_decorated_marks_screenshot}


@contextmanager
def cursorless_recording_paused():
    actions.user.vscode("cursorless.pauseRecording")
    yield
    actions.user.vscode("cursorless.resumeRecording")


def capture_screen(directory: Path, start_time: float):
    timestamp = time.perf_counter() - start_time

    if screenshot_time_stamp_only.get():
        filename = None
    else:
        img = screen.capture_rect(screen.main_screen().rect)
        date = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S-%f")
        filename = f"{date}.png"
        path = directory / filename
        # NB: Writing the image to the file is expensive so we do it asynchronously
        cron.after("50ms", lambda: img.write_file(path))

    return {
        "filename": filename,
        "timeOffset": timestamp,
    }


def extract_decorated_marks(parsed: Iterable[list[Any]]):
    for capture_list in parsed:
        for capture in capture_list:
            items = capture if isinstance(capture, list) else [capture]
            for item in items:
                try:
                    type = item["type"]
                except (KeyError, TypeError):
                    continue

                if type not in {"primitive", "list", "range"}:
                    continue

                yield from extract_decorated_marks_from_target(item)


def extract_decorated_marks_from_target(target: dict):
    type = target["type"]

    if type == "primitive":
        yield from extract_decorated_marks_from_primitive_target(target)
    elif type == "range":
        yield from extract_decorated_marks_from_primitive_target(target["start"])
        yield from extract_decorated_marks_from_primitive_target(target["end"])
    elif type == "list":
        for element in target["elements"]:
            yield from extract_decorated_marks_from_target(element)


def extract_decorated_marks_from_primitive_target(target: dict):
    try:
        mark = target["mark"]
    except KeyError:
        return

    if mark["type"] == "decoratedSymbol":
        yield mark


last_phrase = None


def on_phrase(j):
    actions.user.maybe_capture_phrase(j)


def on_post_phrase(j):
    actions.user.maybe_capture_post_phrase(j)


speech_system.register("pre:phrase", on_phrase)
speech_system.register("post:phrase", on_post_phrase)
