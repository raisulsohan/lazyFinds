"""A GPU that won't start mustn't stop search: the engine falls back to the next backend, the processor last.
Without the model (each start is faked)."""

import pytest

from lazyfinds import engine as E
from lazyfinds import paths


@pytest.fixture
def nvidia_pc(monkeypatch):
    """An NVIDIA PC with both llama.cpp builds and the model downloaded."""
    monkeypatch.setattr(paths, "has_nvidia", lambda: True)
    monkeypatch.setattr(paths, "backend_ready", lambda b: True)
    monkeypatch.setattr(paths, "models_ready", lambda: True)
    monkeypatch.setattr(E, "FAILED", set())
    monkeypatch.setattr(E, "FETCH", "")


def test_order(nvidia_pc, monkeypatch):
    assert E.backends("auto") == ["cuda", "vulkan", "cpu"]
    assert E.backends("vulkan") == ["vulkan", "cuda", "cpu"]
    assert E.backends("cpu") == ["cpu"]
    E.FAILED.add("cuda")
    assert E.backends("auto") == ["vulkan", "cpu"]
    monkeypatch.setattr(paths, "has_nvidia", lambda: False)
    monkeypatch.setattr(paths, "backend_ready", lambda b: b == "vulkan")
    E.FAILED.clear()
    assert E.backends("cuda") == ["vulkan", "cpu"]   # asked for a build that isn't there


def starts_only_on(monkeypatch, working: set[str]) -> list[str]:
    tried = []

    def start(self):
        tried.append(self.backend)
        if self.backend not in working:
            raise E.EngineError(f"llama-server on {self.backend} stopped while loading (exit 1)")

    monkeypatch.setattr(E.Engine, "_start", start)
    return tried


def test_falls_back_to_the_processor(nvidia_pc, monkeypatch, tmp_path):
    tried = starts_only_on(monkeypatch, {"cpu"})
    engine = E.Engine(text_only=True, log=tmp_path / "server.log", backend="auto")
    assert (engine.backend, tried, E.FAILED) == ("cpu", ["cuda", "vulkan", "cpu"], {"cuda", "vulkan"})
    engine.close()
    tried.clear()
    E.Engine(text_only=True, log=tmp_path / "server.log", backend="auto").close()
    assert tried == ["cpu"]   # a GPU that failed isn't tried again in this run


def test_vulkan_when_cuda_fails(nvidia_pc, monkeypatch, tmp_path):
    starts_only_on(monkeypatch, {"vulkan", "cpu"})
    with E.Engine(text_only=True, log=tmp_path / "server.log", backend="auto") as engine:
        assert engine.backend == "vulkan"


def test_processor_failing_is_the_error(nvidia_pc, monkeypatch, tmp_path):
    starts_only_on(monkeypatch, set())
    with pytest.raises(E.EngineError, match="on cpu"):
        E.Engine(text_only=True, log=tmp_path / "server.log", backend="auto")


def test_cuda_failing_fetches_vulkan_and_the_processor_moves_to_it(nvidia_pc, monkeypatch, tmp_path):
    vulkan = {"ready": False}
    monkeypatch.setattr(paths, "backend_ready", lambda b: b == "cuda" or vulkan["ready"])
    fetched = []
    monkeypatch.setattr(E, "_fetch_vulkan", lambda: fetched.append(True))
    starts_only_on(monkeypatch, {"vulkan", "cpu"})
    engine = E.Engine(text_only=True, log=tmp_path / "server.log", backend="auto")
    assert (engine.backend, fetched, engine.outdated) == ("cpu", [True], False)
    vulkan["ready"] = True   # the download is in place
    assert engine.outdated
    engine.close()
    with E.Engine(text_only=True, log=tmp_path / "server.log", backend="auto") as engine:
        assert (engine.backend, engine.outdated) == ("vulkan", False)


def test_no_fetch_when_vulkan_is_there(nvidia_pc, monkeypatch, tmp_path):
    fetched = []
    monkeypatch.setattr(E, "_fetch_vulkan", lambda: fetched.append(True))
    starts_only_on(monkeypatch, {"cpu"})
    E.Engine(text_only=True, log=tmp_path / "server.log", backend="auto").close()
    assert fetched == []   # Vulkan was there and failed too: the processor it is


def test_vulkan_is_fetched_once_a_run(monkeypatch):
    import threading

    from lazyfinds import setup

    monkeypatch.setattr(E, "FETCH", "")
    calls = []
    monkeypatch.setattr(setup, "run", lambda only: calls.append([i.name for i in only]))
    for _ in range(2):
        E._fetch_vulkan()
        for thread in threading.enumerate():
            if thread.name == "fetch-vulkan":
                thread.join(5)
    assert (calls, E.FETCH) == ([[paths.LLAMA_VULKAN.name]], "ready")


def test_a_build_appears_whole(monkeypatch, tmp_path):
    """The Vulkan build is unpacked beside its folder and renamed into place; a second archive for a folder (CUDA's
    runtime libraries) adds its files to it."""
    import hashlib
    import zipfile

    from lazyfinds import setup

    monkeypatch.setenv("LAZYFINDS_HOME", str(tmp_path / "home"))

    def archive(name: str, files: list[str]) -> paths.Pinned:
        path = tmp_path / name
        with zipfile.ZipFile(path, "w") as z:
            for f in files:
                z.writestr(f, f"contents of {f}")
        data = path.read_bytes()
        return paths.Pinned(name, path.as_uri(), len(data), hashlib.sha256(data).hexdigest(), "runtime/vulkan")

    build = archive("build.zip", ["llama-server.exe", "ggml-vulkan.dll", "llama.dll"])
    extra = archive("extra.zip", ["cudart.dll"])
    setup.run(only=[build])
    folder = tmp_path / "home" / "runtime" / "vulkan"
    assert sorted(p.name for p in folder.iterdir()) == [".build.zip.ok", "ggml-vulkan.dll", "llama-server.exe",
                                                         "llama.dll"]
    assert paths.backend_ready("vulkan") and not (tmp_path / "home" / "runtime" / "vulkan.part").exists()
    setup.run(only=[extra])
    assert {".extra.zip.ok", "cudart.dll", "llama.dll"} <= {p.name for p in folder.iterdir()}
    assert not (tmp_path / "home" / "downloads").exists()


def test_reason_is_the_last_error_of_this_start(tmp_path):
    log = tmp_path / "server.log"
    log.write_bytes(b"0.00.010.000 E an older start's error\n")
    since = log.stat().st_size
    with open(log, "ab") as f:
        f.write(b"0.00.104.900 E ggml_cuda_init: failed to initialize CUDA: no CUDA-capable device is detected\n"
                b"error while handling argument \"--device\": invalid device: CUDA0\n\nusage:\n"
                b"0.00.200.000 I srv  init: some information\n")
    assert E._reason(log, since) == 'error while handling argument "--device": invalid device: CUDA0'
    assert E._reason(log, log.stat().st_size) == ""
    assert E._reason(tmp_path / "missing.log", 0) == ""
