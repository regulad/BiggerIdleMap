"""Build the biggerIdleMap release.

  uv run build.py               # -> dist/biggerIdleMap-<version>.zip, containing pak01_dir.vpk

The game's own hud.xml is decompiled from your Deadlock install on every build and patched in three
places, so after a Deadlock update a rebuild is usually all it takes. If Valve changes the markup a
patch anchors on, the build stops and says which:

  1. <styles>: include our stylesheet after hud.vcss.
  2. <scripts>: include our script.
  3. <GlobalClassListener id="minimap_persp">: add a child listener that puts the engine's
     gInCombat and gScoreboardOpen global classes on #minimap_persp.

The enlarged size is checked against the vanilla minimap geometry in the game's hud.css.
Everything under src/ is compiled as is.
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path

import vpk

import panorama

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
BUILD = ROOT / "build"
DIST = ROOT / "dist"
TOOLS = ROOT / ".tools"

HUD_XML = "panorama/layout/hud.vxml_c"
HUD_CSS = "panorama/styles/hud.vcss_c"
STYLESHEET = "s2r://panorama/styles/bigger_idle_map.vcss"
SCRIPT = "s2r://panorama/scripts/bigger_idle_map.vts"
HUD_HEIGHT = 1080  # Panorama lays the HUD out on a canvas 1080 units tall at every resolution.

# Source2Viewer-CLI (ValveResourceFormat, MIT) decompiles the game's own panorama files. It is
# fetched on first use into .tools/, pinned by version and sha256.
VRF_VERSION = "20.1"
VRF_URL = (f"https://github.com/ValveResourceFormat/ValveResourceFormat/releases/download/"
           f"{VRF_VERSION}/cli-windows-x64.zip")
VRF_SHA256 = "67aaf8a67a85ca60fd09ff343e62103a4cdf3a2d177fc656231d7d734c5b3aa3"


def find_deadlock(explicit: str | None) -> Path:
    """The Deadlock install root (the folder containing game/)."""
    if explicit or os.environ.get("DEADLOCK_PATH"):
        candidates = [Path(explicit or os.environ["DEADLOCK_PATH"])]
    else:
        steam_roots = []
        if sys.platform == "win32":
            import winreg
            for hive, key, value in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                                     (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        steam_roots.append(Path(winreg.QueryValueEx(k, value)[0]))
                except OSError:
                    pass
        libraries = []
        for steam in steam_roots:
            libraries.append(steam)
            vdf = steam / "steamapps/libraryfolders.vdf"
            if vdf.is_file():
                for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace")):
                    libraries.append(Path(m.group(1).replace("\\\\", "\\")))
        candidates = [lib / "steamapps/common/Deadlock" for lib in libraries]
    for c in candidates:
        if (c / "game/citadel/gameinfo.gi").is_file():
            return c
    raise SystemExit("Deadlock not found; pass --game <path to the Deadlock folder> or set DEADLOCK_PATH.")


def ensure_vrf() -> Path:
    exe = TOOLS / f"vrf-{VRF_VERSION}" / "Source2Viewer-CLI.exe"
    if exe.exists():
        return exe
    if sys.platform != "win32":
        raise SystemExit("Only the Windows build of Source2Viewer-CLI is pinned; pass --vrf <path to Source2Viewer-CLI>.")
    print(f"fetching Source2Viewer-CLI {VRF_VERSION} ...")
    with urllib.request.urlopen(VRF_URL) as resp:
        data = resp.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != VRF_SHA256:
        raise SystemExit(f"Source2Viewer-CLI download hash mismatch: got {digest}, expected {VRF_SHA256}")
    exe.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as f:
        f.write(data)
        zipfile.ZipFile(f).extractall(exe.parent)
    return exe


def decompile(vrf: Path, out: Path, *args: str) -> str:
    """Run Source2Viewer-CLI with -o out -d and return the decompiled text."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    r = subprocess.run([str(vrf), *args, "-o", str(out), "-d"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not out.is_file():
        raise SystemExit(f"Source2Viewer-CLI failed ({r.returncode}) on {' '.join(args)}:\n{r.stdout}\n{r.stderr}")
    return out.read_text(encoding="utf-8")


def patch_hud_xml(text: str) -> str:
    # Drop the "xml reconstructed by Source 2 Viewer" banner.
    text = re.sub(r"\A\s*<!--.*?-->\s*", "", text, flags=re.S)

    anchor = re.compile(r'^([ \t]*)<include src="s2r://panorama/styles/hud\.vcss" />[ \t]*$', re.M)
    if len(anchor.findall(text)) != 1:
        raise SystemExit('hud.xml: expected exactly one <include src="s2r://panorama/styles/hud.vcss" />')
    text = anchor.sub(lambda m: f'{m.group(0)}\n{m.group(1)}<include src="{STYLESHEET}" />', text)

    scripts = re.compile(r'^([ \t]*)<scripts>[ \t]*$', re.M)
    styles_end = re.compile(r'^([ \t]*)</styles>[ \t]*$', re.M)
    if len(scripts.findall(text)) == 1:
        text = scripts.sub(lambda m: f'{m.group(0)}\n{m.group(1)}\t<include src="{SCRIPT}" />', text)
    elif len(scripts.findall(text)) == 0 and len(styles_end.findall(text)) == 1:
        text = styles_end.sub(lambda m: f'{m.group(0)}\n{m.group(1)}<scripts>\n{m.group(1)}\t<include src="{SCRIPT}" />'
                                        f'\n{m.group(1)}</scripts>', text)
    else:
        raise SystemExit("hud.xml: expected one </styles> and at most one <scripts>")

    # A GlobalClassListener mirrors global classes onto its parent panel (vanilla styles
    # CitadelHudMovementSpeed.gShopOpen for <GlobalClassListener id="movespeed_container">), so a
    # childless listener inside #minimap_persp puts gInCombat and gScoreboardOpen on #minimap_persp itself.
    persp = re.compile(r'^([ \t]*)<GlobalClassListener\b[^>]*\bid="minimap_persp"[^>]*(?<!/)>[ \t]*$', re.M)
    if len(persp.findall(text)) != 1:
        raise SystemExit('hud.xml: expected exactly one <GlobalClassListener id="minimap_persp"> with children')
    return persp.sub(lambda m: f'{m.group(0)}\n{m.group(1)}\t<GlobalClassListener hittest="false" '
                               f'classes="gInCombat gScoreboardOpen" />', text)


def css_length(hud_css: str, selector: str, prop: str, unit: str, default: float | None = None) -> float:
    """A length from a vanilla rule, e.g. ("#minimap_persp", "height", "px") -> 520."""
    block = re.search(rf"^{re.escape(selector)}\s*\{{([^}}]*)\}}", hud_css, re.M)
    value = block and re.search(rf"^\s*{re.escape(prop)}:\s*(-?[\d.]+){re.escape(unit)};", block.group(1), re.M)
    if value:
        return float(value.group(1))
    if default is not None:
        return default
    raise SystemExit(f"hud.css: no '{prop}: <n>{unit}' in the {selector} rule")


def enlarged_geometry(hud_css: str, scale: float) -> dict[str, str]:
    """ui-scale for the enlarged #minimap_persp, checked against the screen height. The widget
    (520 tall as of 2026-10, including room for the hints above the map) keeps its bottom margin,
    which is in its parent's units and so isn't scaled."""
    height = css_length(hud_css, "#minimap_persp", "height", "px")
    margin = css_length(hud_css, "#minimap_persp", "margin-bottom", "px", 0)
    most = (HUD_HEIGHT - margin) / height
    if scale > most:
        raise SystemExit(f"--scale {scale} makes the minimap taller than the screen; the most that fits is {most:.2f}")
    return {"PERSP_UI_SCALE": f"{scale * 100:.2f}%"}


def syntax_check(rel: str, text: str) -> None:
    """Fail the build on a JS syntax error, if node is available."""
    node = shutil.which("node")
    if not node:
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "check.js"
        path.write_text(text, encoding="utf-8")
        r = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"JS syntax error in {rel}:\n{r.stderr}")


def render(text: str, values: dict[str, str]) -> str:
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: values[m.group(1)], text)


def build(deadlock: Path, vrf: Path, scale: float) -> Path:
    shutil.rmtree(BUILD, ignore_errors=True)
    stage = BUILD / "stage"
    stage.mkdir(parents=True)
    work = BUILD / "work"
    game_vpk = str(deadlock / "game/citadel/pak01_dir.vpk")

    # Sources: the game's patched hud.xml plus everything under src/. With a filter matching exactly
    # one file, Source2Viewer-CLI's -o names the output file itself.
    sources: dict[str, str] = {}
    vanilla_xml = decompile(vrf, work / "vanilla/hud.xml", "-i", game_vpk, "-f", HUD_XML)
    sources["panorama/layout/hud.xml"] = patch_hud_xml(vanilla_xml)
    vanilla_css = decompile(vrf, work / "vanilla/hud.css", "-i", game_vpk, "-f", HUD_CSS)
    values = enlarged_geometry(vanilla_css, scale)
    for path in sorted(SRC.rglob("*")):
        if path.is_file():
            sources[path.relative_to(SRC).as_posix()] = render(path.read_text(encoding="utf-8"), values)

    for rel, text in sources.items():
        if rel.endswith(".vts"):
            syntax_check(rel, text)
        out_rel, data = panorama.compile_text(rel, text)
        dest = stage / out_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        print(f"  {rel} -> {out_rel} ({len(data)} bytes)")

        # Read it back with an independent parser before shipping it.
        back = decompile(vrf, work / "readback" / rel, "-i", str(dest))
        for marker in ("gInCombat", "gScoreboardOpen", STYLESHEET, SCRIPT, "biggerIdleMapEnlarged"):
            if marker in text and marker not in back:
                raise SystemExit(f"read-back of {out_rel} lost {marker!r}; the container format is off")

    out_vpk = BUILD / "pak01_dir.vpk"
    vpk.new(str(stage)).save(str(out_vpk))

    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    DIST.mkdir(exist_ok=True)
    release = DIST / f"biggerIdleMap-{version}.zip"
    with zipfile.ZipFile(release, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(out_vpk, out_vpk.name)
    print(f"packed -> {release} ({release.stat().st_size} bytes)")
    return release


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game", help="Deadlock install folder (default: found via Steam, or DEADLOCK_PATH)")
    ap.add_argument("--vrf", help="path to Source2Viewer-CLI (default: fetched into .tools/)")
    ap.add_argument("--scale", type=float, default=1.5,
                    help="minimap size while you're not fighting, relative to vanilla (default 1.5)")
    args = ap.parse_args()
    if args.scale <= 0:
        ap.error("--scale must be positive")

    deadlock = find_deadlock(args.game)
    vrf = Path(args.vrf) if args.vrf else ensure_vrf()
    build(deadlock, vrf, args.scale)


if __name__ == "__main__":
    main()
