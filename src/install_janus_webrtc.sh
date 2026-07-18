#!/bin/bash
# =============================================================
#  MagicBridge — Janus WebRTC + H.264 ustreamer add-on installer
#
#  Adds the C790/CSI hardware-H.264 + WebRTC video path alongside the
#  existing MJPEG/MS2109 path. Does NOT touch, disable, or remove
#  anything the current MJPEG stream depends on — this is purely
#  additive. video.py only calls into this new path when mode="h264"
#  is explicitly requested (default stays "mjpeg").
#
#  Run as root:
#    sudo bash install_janus_webrtc.sh
#
#  Build order matters and is the thing this script got wrong on the
#  first pass (see docs/h264.md in pikvm/ustreamer, and
#  github.com/pikvm/ustreamer/issues/134):
#    ustreamer's WITH_JANUS=1 build compiles a plugin
#    (janus/libjanus_ustreamer.so) that #includes Janus Gateway's own
#    C headers (<janus/plugins/plugin.h> etc). Those headers only
#    exist once Janus itself has been built+installed first. Building
#    ustreamer WITH_JANUS=1 before Janus exists fails with
#    "Makefile:79: janus] Error 2" (fatal error: refcount.h: No such
#    file or directory) — that's exactly what happened when this
#    script's sections were in the wrong order.
#
#    Debian/Raspberry Pi OS's apt "janus" package also isn't a safe
#    substitute: it doesn't ship the C headers, and (per the same
#    ustreamer issue thread) newer Janus API versions changed the RTP
#    relay struct in a way that breaks ustreamer's bundled plugin
#    source. So Janus is built from source here, pinned to the same
#    v1.0.0 tag ustreamer's own docs (docs/h264.md) use for their
#    compatibility example — not apt, not latest master.
#
#  What this does, in dependency order:
#    1. Install build deps (for both Janus and ustreamer)
#    2. Build + install Janus Gateway from source (tag v1.0.0) to
#       /opt/janus — this is what provides the C headers ustreamer
#       needs, and the janus binary + plugins/configs directories.
#    3. Symlink /usr/include/janus -> /opt/janus/include/janus and
#       apply the one-line header fix docs/h264.md calls for
#       (plugin.h's "#include refcount.h" needs to be "../refcount.h"
#       to resolve from a third-party build tree).
#    4. Build ustreamer WITH_JANUS=1 (headers now available) ->
#       /usr/local/bin/ustreamer, which comes before /usr/bin/ustreamer
#       in PATH, so shutil.which("ustreamer") in video.py picks up the
#       new build automatically. The old apt-installed /usr/bin/ustreamer
#       is left untouched — removing /usr/local/bin/ustreamer instantly
#       reverts to the working MJPEG-only binary with zero other
#       changes needed.
#    5. Install the built janus/libjanus_ustreamer.so into Janus's
#       plugin dir + write janus.plugin.ustreamer.jcfg pointing at the
#       same memsink name video.py's h264 mode uses.
#    6. Create (but do not start) a janus-webrtc systemd service.
#    7. Vendor the matching janus.js browser client (same v1.0.0 tag)
#       + webrtc-adapter into /opt/magicbridge/web/static/vendor/ so
#       index_v13.html's WebRTC toggle has something to load.
#
#  Safe to run before the C790 board physically exists: nothing here
#  captures video or touches /dev/video*. It only installs software.
#
#  KNOWN RISK, flagged honestly rather than hidden: even with the
#  header/build-order issue fixed, ustreamer's bundled Janus plugin
#  source (last touched years ago) may still not compile clean against
#  janus-gateway v1.0.0 depending on exact compiler/glib versions on
#  this Pi — the GitHub issue this fix is based on shows a *second*,
#  deeper class of failure (janus_plugin_rtp API mismatch) on some
#  Janus versions that would require patching ustreamer's janus/src/
#  plugin.c. If the build below still fails at the "janus" make
#  target, that's this deeper issue — capture the build log and treat
#  it as a hardware-arrival-time fix, per plan. The core H.264 capture
#  itself (ustreamer --h264-sink, no Janus involved) does not depend
#  on this and will still work either way.
# =============================================================
set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
info() { echo -e "${BLUE}→${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
die()  { echo -e "${RED}✗ FATAL:${NC} $*"; }

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash install_janus_webrtc.sh"; exit 1; }

MEMSINK_NAME="magicbridge::h264"
BUILD_DIR="/tmp/mb_janus_build"
JANUS_PREFIX="/opt/janus"
JANUS_TAG="v1.0.0"   # pinned to match the ustreamer docs/h264.md compat example
# ustreamer is pinned to the SAME version as the running binary so the janus
# plugin (memsink consumer) and the /usr/local/bin/ustreamer producer speak the
# same memsink protocol. HEAD drifts ahead of Janus v1.0.0's plugin API.
USTREAMER_TAG="v6.61"

# Always start from a clean build tree. This script has already been run
# once on this Pi with a build-order bug (ustreamer built before Janus
# existed, so its WITH_JANUS build failed partway through and left a
# partial ustreamer/ checkout+object files behind). Reusing that directory
# risks stale/incremental build state masking whether today's fix actually
# works. Re-cloning is cheap next to the actual compile time.
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

echo -e "${BOLD}MagicBridge — Janus WebRTC / H.264 add-on installer${NC}"
echo ""

# ══════════════════════════════════════════════════════════════════
# 0. Clean up after the first (buggy) run of this script, which
#    installed Debian's apt "janus" package as a fallback. That apt
#    unit would otherwise fight our source-built Janus for the same
#    ports once janus-webrtc.service is started later. Not removing
#    the package itself (shared libs it pulled in are harmless to
#    keep) — just making sure its service isn't live.
# ══════════════════════════════════════════════════════════════════
if systemctl list-unit-files 2>/dev/null | grep -q '^janus\.service'; then
    info "Disabling apt's janus.service (superseded by our source build + janus-webrtc.service)..."
    systemctl stop janus.service 2>/dev/null || true
    systemctl disable janus.service 2>/dev/null || true
fi

# ══════════════════════════════════════════════════════════════════
# 1. Build dependencies (Janus + ustreamer's WITH_JANUS need)
# ══════════════════════════════════════════════════════════════════
info "Installing build dependencies..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential git pkg-config cmake meson ninja-build gengetopt \
    automake libtool libtool-bin \
    libevent-dev libjpeg-dev libbsd-dev \
    libssl-dev libsofia-sip-ua-dev libglib2.0-dev libopus-dev \
    libogg-dev libcurl4-openssl-dev liblua5.3-dev libconfig-dev \
    libwebsockets-dev libnice-dev libsrtp2-dev libmicrohttpd-dev \
    libjansson-dev libusrsctp-dev \
    libasound2-dev libspeex-dev libspeexdsp-dev \
    2>&1 | tail -20
ok "Build dependencies installed (or already present)"

# ══════════════════════════════════════════════════════════════════
# 2. Janus Gateway from source, pinned tag, installed to /opt/janus
#    (must happen BEFORE the ustreamer WITH_JANUS build — see header
#    comment above for why)
# ══════════════════════════════════════════════════════════════════
info "Building Janus Gateway ($JANUS_TAG) -> $JANUS_PREFIX ..."
info "(This step can take 15-30+ minutes on a Pi 4; safe to leave running.)"
JANUS_OK=false
cd "$BUILD_DIR"
if [[ ! -d janus-gateway ]]; then
    git clone --branch "$JANUS_TAG" --depth 1 https://github.com/meetecho/janus-gateway.git 2>&1 | tail -5
fi
cd janus-gateway
sh autogen.sh 2>&1 | tail -15
./configure --prefix="$JANUS_PREFIX" \
    --disable-all-transports --enable-websockets \
    --disable-plugin-videoroom --disable-plugin-audiobridge \
    --disable-plugin-recordplay --disable-plugin-sip \
    --disable-plugin-nosip --disable-plugin-streaming \
    --disable-plugin-videocall --disable-plugin-textroom \
    --disable-plugin-echotest \
    2>&1 | tail -40
make -j"$(nproc)" 2>&1 | tail -80
make install 2>&1 | tail -30
make configs 2>&1 | tail -10

if [[ -f "$JANUS_PREFIX/bin/janus" ]]; then
    JANUS_OK=true
    ln -sf "$JANUS_PREFIX/bin/janus" /usr/bin/janus 2>/dev/null || true
    ok "Janus Gateway ($JANUS_TAG) built and installed to $JANUS_PREFIX"
else
    die "Janus source build failed — check the build log above. This does not touch the current MJPEG stream in any way; ustreamer's own WITH_JANUS build (next step) will be skipped since it needs Janus's headers."
fi

JANUS_PLUGIN_DIR="$JANUS_PREFIX/lib/janus/plugins"
# Janus's compiled-in default config dir is etc/janus (what `make configs`
# populates and what the running gateway reads — see janus.jcfg
# configs_folder). The plugin config MUST live here. An earlier version of this
# script used "$JANUS_PREFIX/lib/janus/configs", which never existed, so the
# plugin loaded with no config ("Missing config value: video.sink") and the
# janus-webrtc unit flapped.
JANUS_CONF_DIR="$JANUS_PREFIX/etc/janus"
JANUS_PC="$JANUS_PREFIX/lib/pkgconfig/janus-gateway.pc"
JANUS_BIN="$JANUS_PREFIX/bin/janus"

# ══════════════════════════════════════════════════════════════════
# 3. Make Janus's C headers visible where ustreamer's build expects
#    them, with the one-line relative-include fix docs/h264.md calls
#    for. Idempotent: safe to re-run.
# ══════════════════════════════════════════════════════════════════
if [[ "$JANUS_OK" == true ]]; then
    info "Wiring Janus headers into /usr/include/janus for ustreamer's build..."
    if [[ ! -e /usr/include/janus ]]; then
        ln -s "$JANUS_PREFIX/include/janus" /usr/include/janus
        ok "Symlinked /usr/include/janus -> $JANUS_PREFIX/include/janus"
    else
        ok "/usr/include/janus already present, leaving as-is"
    fi
    if [[ -f /usr/include/janus/plugins/plugin.h ]] \
        && ! grep -q '\.\./refcount\.h' /usr/include/janus/plugins/plugin.h; then
        sed -i -e 's|^#include "refcount.h"$|#include "../refcount.h"|g' \
            /usr/include/janus/plugins/plugin.h
        ok "Patched plugin.h include path (refcount.h -> ../refcount.h)"
    fi

    # ustreamer's janus/Makefile gets its cflags via `pkg-config janus-gateway`,
    # which is what pulls in glib-2.0's include path (the janus headers do
    # `#include <glib.h>`). Janus's own build does NOT reliably drop a
    # janus-gateway.pc into $JANUS_PREFIX/lib/pkgconfig on this Pi, so without
    # this the plugin build dies with "glib.h: No such file or directory".
    # Create it so `PKG_CONFIG_PATH=$JANUS_PREFIX/lib/pkgconfig pkg-config
    # --cflags janus-gateway` resolves glib + jansson.
    if [[ ! -f "$JANUS_PC" ]]; then
        mkdir -p "$(dirname "$JANUS_PC")"
        cat > "$JANUS_PC" <<PC
prefix=$JANUS_PREFIX
exec_prefix=\${prefix}
libdir=\${exec_prefix}/lib
includedir=\${prefix}/include

Name: janus-gateway
Description: Janus WebRTC Server
Version: 1.0.0
Requires: glib-2.0 jansson
Cflags: -I\${includedir}/janus
Libs: -L\${libdir}
PC
        ok "Wrote $JANUS_PC (glib/jansson cflags for the ustreamer plugin build)"
    fi
fi
export PKG_CONFIG_PATH="$JANUS_PREFIX/lib/pkgconfig:${PKG_CONFIG_PATH:-}"

# ══════════════════════════════════════════════════════════════════
# 4. ustreamer WITH_JANUS=1 (source build — the apt package lacks this)
#    Only attempted if Janus's headers are actually in place.
# ══════════════════════════════════════════════════════════════════
JANUS_PLUGIN_SO=""
if [[ "$JANUS_OK" == true ]]; then
    info "Building ustreamer with Janus plugin support..."
    cd "$BUILD_DIR"
    if [[ ! -d ustreamer ]]; then
        # Pin to $USTREAMER_TAG (matches the running binary => same memsink
        # protocol). HEAD is unsafe: it targets a newer Janus plugin API.
        git clone --depth 1 --branch "$USTREAMER_TAG" \
            https://github.com/pikvm/ustreamer.git 2>&1 | tail -5
    fi
    cd ustreamer
    # API-skew patch: ustreamer's janus plugin sets packet.extensions.abs_capture_ts,
    # a field absent from Janus v1.0.0's janus_plugin_rtp_extensions struct, so the
    # build fails with "has no member named 'abs_capture_ts'". It's a non-essential
    # capture-time RTP header extension (receiver-side latency/A-V sync estimation);
    # neuter the single assignment so the plugin compiles. Video path unaffected.
    if grep -q 'abs_capture_ts = rtp.grab_ntp_ts;' janus/src/client.c; then
        sed -i 's|packet.extensions.abs_capture_ts = rtp.grab_ntp_ts;|/* MagicBridge: abs_capture_ts absent from installed Janus RTP struct (API skew); non-essential capture-time RTP ext, video path unaffected */|' janus/src/client.c
        ok "Patched out abs_capture_ts (Janus v1.0.0 API compatibility)"
    fi
    make WITH_JANUS=1 WITH_GPIO=0 WITH_SYSTEMD=0 -j"$(nproc)" 2>&1 | tail -60
    if [[ -f ustreamer ]]; then
        install -m 755 ustreamer /usr/local/bin/ustreamer
        ok "ustreamer (Janus-enabled) installed to /usr/local/bin/ustreamer"
        /usr/local/bin/ustreamer --version || true
    else
        die "ustreamer build failed — check the build log above. /usr/bin/ustreamer (MJPEG-only, apt) is untouched, current stream unaffected."
    fi

    if [[ -f janus/libjanus_ustreamer.so ]]; then
        JANUS_PLUGIN_SO="$BUILD_DIR/ustreamer/janus/libjanus_ustreamer.so"
        ok "Janus ustreamer plugin built: $JANUS_PLUGIN_SO"
    else
        warn "janus/libjanus_ustreamer.so not produced even with headers present."
        warn "This is the deeper API-compatibility class of failure described in"
        warn "the header comment above (github.com/pikvm/ustreamer/issues/134,"
        warn "janus_plugin_rtp mismatch) — capture the build log and treat as a"
        warn "hardware-arrival-time fix. --h264-sink itself (no Janus) is unaffected."
    fi
else
    warn "Skipping ustreamer WITH_JANUS build — Janus isn't installed/headers missing."
fi

# ══════════════════════════════════════════════════════════════════
# 5. Wire the ustreamer plugin into Janus, pointed at our memsink
# ══════════════════════════════════════════════════════════════════
if [[ -n "$JANUS_PLUGIN_SO" && -d "$JANUS_PLUGIN_DIR" ]]; then
    install -m 755 "$JANUS_PLUGIN_SO" "$JANUS_PLUGIN_DIR/libjanus_ustreamer.so"
    ok "Installed Janus ustreamer plugin to $JANUS_PLUGIN_DIR"

    mkdir -p "$JANUS_CONF_DIR"
    # Config schema per ustreamer v6.61 janus/src/config.c: the memsink name is
    # "video.sink" (section "video", option "sink") — REQUIRED. (Older docs said
    # "memsink.object"; that key does not exist in v6.61 and yields "Missing
    # config value: video.sink".) Optional audio capture is "acap.device".
    #
    # VIDEO-ONLY here: the C790 I2S audio path is a known upstream driver
    # dead-end on the DIY Pi (the V4L2 driver detects audio but never programs
    # the chip's I2S output regs — see docs/DIY_PROGRESS.md). An "acap.device"
    # pointing at a non-existent ALSA card would just error; video is
    # unaffected. Revisit only if upstream fixes the driver.
    cat > "$JANUS_CONF_DIR/janus.plugin.ustreamer.jcfg" <<EOF
# MagicBridge C790/H.264 -> WebRTC bridge (DIY), ustreamer janus plugin v6.61.
# video.py's h264 mode feeds this memsink name (--h264-sink ${MEMSINK_NAME}); keep in sync.
video: {
    sink = "${MEMSINK_NAME}"
}
EOF
    ok "Wrote $JANUS_CONF_DIR/janus.plugin.ustreamer.jcfg (video.sink=${MEMSINK_NAME}, video-only)"
else
    warn "Skipping Janus plugin wiring (plugin .so not built or plugin dir missing)."
fi

# ══════════════════════════════════════════════════════════════════
# 6. systemd service for Janus (separate from magicbridge/mb-gadget)
# ══════════════════════════════════════════════════════════════════
cat > /etc/systemd/system/janus-webrtc.service <<EOF
[Unit]
Description=Janus WebRTC Gateway (MagicBridge C790 video path)
After=network.target

[Service]
ExecStart=${JANUS_BIN} --disable-colors --log-stdout --configs-folder ${JANUS_CONF_DIR} --plugins-folder ${JANUS_PLUGIN_DIR}
Restart=on-failure
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable janus-webrtc.service 2>&1 | tail -5

info "Not starting janus-webrtc.service yet — there's no capture device feeding"
info "the memsink until the C790 is physically installed and mode=h264 is set."
info "Once hardware arrives: systemctl start janus-webrtc, then set mode=h264"
info "via the existing /api/stream/settings endpoint (or the UI toggle)."

# ══════════════════════════════════════════════════════════════════
# 7. janus.js + adapter.js browser clients (needed by index_v13.html's
#    WebRTC path). Pinned to the same versions pikvm/ustreamer's
#    docs/h264.md demo uses, to match the server API we just built.
# ══════════════════════════════════════════════════════════════════
info "Fetching matching janus.js + adapter.js browser clients..."
VENDOR_DIR="/opt/magicbridge/web/static/vendor"
mkdir -p "$VENDOR_DIR"

JANUS_JS_SRC="$BUILD_DIR/janus-gateway/html/janus.js"
if [[ -f "$JANUS_JS_SRC" ]]; then
    install -m 644 "$JANUS_JS_SRC" "$VENDOR_DIR/janus.js"
    ok "Copied janus.js ($JANUS_TAG, from the source checkout) -> $VENDOR_DIR/janus.js"
elif curl -fsSL -o "$VENDOR_DIR/janus.js" \
    "https://raw.githubusercontent.com/meetecho/janus-gateway/${JANUS_TAG}/html/janus.js"; then
    ok "Downloaded janus.js ($JANUS_TAG) -> $VENDOR_DIR/janus.js"
else
    warn "Could not get janus.js. Frontend WebRTC toggle will silently stay on"
    warn "MJPEG (it checks 'typeof Janus === undefined' and no-ops) until"
    warn "$VENDOR_DIR/janus.js exists. Fetch it manually later:"
    warn "  curl -o $VENDOR_DIR/janus.js https://raw.githubusercontent.com/meetecho/janus-gateway/${JANUS_TAG}/html/janus.js"
fi

# webrtc-adapter 8.1.0 — the specific version pikvm/ustreamer's docs pin.
if curl -fsSL -o "$VENDOR_DIR/adapter.js" \
    "https://webrtc.github.io/adapter/adapter-8.1.0.js"; then
    ok "Downloaded adapter.js (8.1.0) -> $VENDOR_DIR/adapter.js"
else
    warn "Could not fetch adapter.js — some browsers may need it for full WebRTC compat. Non-fatal."
fi

chown -R root:root "$VENDOR_DIR" 2>/dev/null || true
chmod 644 "$VENDOR_DIR"/*.js 2>/dev/null || true

echo ""
ok "Janus WebRTC add-on install finished."
echo "    janus binary:             ${JANUS_BIN} ($JANUS_TAG)"
echo "    janus headers:            $([[ -e /usr/include/janus ]] && echo yes || echo no)"
echo "    ustreamer (Janus build):  $(command -v /usr/local/bin/ustreamer || echo 'NOT FOUND')"
echo "    janus plugin installed:   $([[ -n \"$JANUS_PLUGIN_SO\" ]] && echo yes || echo no)"
echo "    memsink name:             ${MEMSINK_NAME}"
echo "    janus.js vendored:        $([[ -f \"$VENDOR_DIR/janus.js\" ]] && echo yes || echo no)"
echo "    adapter.js vendored:      $([[ -f \"$VENDOR_DIR/adapter.js\" ]] && echo yes || echo no)"
