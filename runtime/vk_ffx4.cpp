// AMD FSR 4 (FSR Upscaling 4.x, the machine-learned upscaler) through the libraries of AMD's FSR SDK 2.4:
// amd_fidelityfx_loader_vk.dll and amd_fidelityfx_upscaler_vk.dll next to the executable, loaded at run time. The SDK's
// headers are in third_party/ffx_sdk (MIT), at the paths they have in the SDK. FSR 3.1 and frame generation stay with
// vk_ffx.cpp and the library of SDK 1.1.4: the two sets of headers name the same types with other layouts, so nothing
// of the API crosses between the two files, and vk_ffx.cpp's vk_ffx_upscale calls in here first.
//
// The loader answers which upscaler versions it can run on a device (ffxQueryDescGetVersions with the device given):
// 4.1.2, 3.1.6 and 2.3.4 on a Radeon the ML upscaler supports (RX 6000, 7000 and 9000 cards and some Ryzen APUs, with
// AMD's driver 26.10.1 or newer on Windows), 3.1.6 and 2.3.4 on any other. FSR 4 runs only where a 4.x version is
// listed, and its contexts are made with that version asked for by its id, so the loader cannot hand out another.
// Where it is not listed, or a context or a dispatch fails, vk_ffx.cpp's FSR 3.1 serves the frame as before.
//
// The inputs are the ones FSR 3.1 takes (vk_ffx.cpp): the scene's light target in linear HDR, its D32F depth (not
// inverted, the far plane at infinity) and the camera motion vectors in render pixels from this frame to the previous
// one. FSR 4 needs no reactive or transparency mask.
#define VK_FFX_NO_API   // vk_ffx.h: without the API declarations of the other set of headers
#ifndef _WIN32
// AMD publishes the SDK's libraries for Windows only: in any other build FSR 4 is never listed, vk_ffx.cpp's FSR 3.1
// path is all there is, and the SDK's headers are not read.
extern "C" {
#include "vk.h"
#include "vk_ffx.h"
}
extern "C" int vk_ffx4_available(void) { return 0; }
extern "C" const char *vk_ffx4_name(void) { return "AMD FSR 4"; }
extern "C" int vk_ffx4_upscale(VkCommandBuffer, const VkFfxImage *, const VkFfxImage *, const VkFfxImage *, const VkFfxImage *,
                               float, float, int, float, float) {
    return 0;
}
extern "C" void vk_ffx4_release(void) {}
#else
#include <windows.h>
#include <atomic>
#include <mutex>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#include "third_party/ffx_sdk/upscalers/include/ffx_upscale.h"
#include "vk_ffx4_api.h"

extern "C" {
#include "cfg.h"
#include "host.h"
#include "platform/platform.h"
#include "vk.h"
#include "vk_ffx.h"
}

namespace {

struct Api {
    PfnFfxCreateContext create = nullptr;
    PfnFfxDestroyContext destroy = nullptr;
    PfnFfxConfigure configure = nullptr;
    PfnFfxQuery query = nullptr;
    PfnFfxDispatch dispatch = nullptr;
} A;

std::mutex g_lock;                // the probe: the menu asks from its thread, the replay from its own
std::atomic<int> g_state{-1};     // -1 not probed; 0 FSR 4 does not run here; 1 it does
uint64_t g_version;               // the id of the 4.x version the loader listed for the device
char g_name[48] = "AMD FSR 4";    // "AMD FSR 4.1.2", for the menu and the log
int g_failures;                   // contexts or dispatches that failed; past FAIL_MAX FSR 4 is off for the run
const int FAIL_MAX = 3;

void message(uint32_t type, const wchar_t *msg) {
    char s[512];
    size_t n = 0;
    for (; msg && msg[n] && n < sizeof s - 1; n++) s[n] = msg[n] < 128 ? (char)msg[n] : '?';
    s[n] = 0;
    static int told;
    if (told++ < 40) rt_log("ffx4: %s %s", type == FFX_API_MESSAGE_TYPE_ERROR ? "error:" : "warning:", s);
}

// a library of the SDK next to the executable; 0 when the path does not fit
int beside_exe(const char *name, char *path, size_t size) {
    DWORD n = GetModuleFileNameA(NULL, path, (DWORD)size);
    while (n && path[n - 1] != '\\') n--;
    return snprintf(path + n, size - n, "%s", name) < (int)(size - n);
}

int file_there(const char *path) {
    DWORD attr = GetFileAttributesA(path);
    return attr != INVALID_FILE_ATTRIBUTES && !(attr & FILE_ATTRIBUTE_DIRECTORY);
}

int g_loader = -1;   // under g_lock: -1 not tried, 0 the loader is not there or does not load, 1 its entry points are in A

// Loads the SDK's loader from beside the executable. Under g_lock, once the device exists (-1 before).
int load(void) {
    if (g_loader >= 0) return g_loader;
    const VkCtx *c = vk_ctx();
    if (!c || !c->device) return -1;   // asked before the device: again later
    char loader[MAX_PATH];
    if (!beside_exe("amd_fidelityfx_loader_vk.dll", loader, sizeof loader) || !file_there(loader))
        return g_loader = 0;   // not shipped beside this build: nothing is said, FSR 3.1 is what the menu offers
    HMODULE m = LoadLibraryA(loader);
    if (!m) {
        rt_log("ffx4: %s does not load (error %lu): FSR 4 off", loader, (unsigned long)GetLastError());
        return g_loader = 0;
    }
    A.create = (PfnFfxCreateContext)(void *)GetProcAddress(m, "ffxCreateContext");
    A.destroy = (PfnFfxDestroyContext)(void *)GetProcAddress(m, "ffxDestroyContext");
    A.configure = (PfnFfxConfigure)(void *)GetProcAddress(m, "ffxConfigure");
    A.query = (PfnFfxQuery)(void *)GetProcAddress(m, "ffxQuery");
    A.dispatch = (PfnFfxDispatch)(void *)GetProcAddress(m, "ffxDispatch");
    if (!A.create || !A.destroy || !A.configure || !A.query || !A.dispatch) {
        rt_log("ffx4: amd_fidelityfx_loader_vk.dll lacks the API entry points: FSR 4 off");
        return g_loader = 0;
    }
    return g_loader = 1;
}

// The versions the loader lists for one kind of context on this device, in its order: their count, at most cap.
// Under g_lock, the loader loaded.
int versions(uint64_t type, uint64_t *ids, const char **names, int cap) {
    const VkCtx *c = vk_ctx();
    VkDeviceContext dc = {c->device, c->phys, vkGetDeviceProcAddr};
    uint64_t n = 0;
    ffxQueryDescGetVersions q = {};
    q.header.type = FFX_API_QUERY_DESC_TYPE_GET_VERSIONS;
    q.createDescType = type;
    q.device = &dc;
    q.outputCount = &n;
    if (A.query(nullptr, &q.header) != FFX_API_RETURN_OK || !n) return 0;   // the count
    if (n > (uint64_t)cap) n = (uint64_t)cap;
    q.versionIds = ids;
    q.versionNames = names;
    if (A.query(nullptr, &q.header) != FFX_API_RETURN_OK) return 0;
    return (int)n;
}

// Asks the loader for the device's upscaler versions. Under g_lock; -1 before the device exists.
int probe(void) {
    int loaded = load();
    if (loaded <= 0) return loaded;
    char upscaler[MAX_PATH];
    if (!beside_exe("amd_fidelityfx_upscaler_vk.dll", upscaler, sizeof upscaler) || !file_there(upscaler)) return 0;
    uint64_t ids[16] = {};
    const char *names[16] = {};
    int n = versions(FFX_API_CREATE_CONTEXT_DESC_TYPE_UPSCALE, ids, names, 16);
    if (!n) {
        rt_log("ffx4: the FSR SDK's loader lists no upscaler for this device: FSR 4 off");
        return 0;
    }
    char list[200] = "";
    size_t len = 0;
    int best = -1, best_major = 0;
    for (int i = 0; i < n; i++) {
        const char *nm = names[i] ? names[i] : "?";
        len += (size_t)snprintf(list + len, sizeof list - len, "%s%s", i ? ", " : "", nm);
        if (len >= sizeof list) len = sizeof list - 1;
        int major = atoi(nm);   // "4.1.2", and "4.1.2 *" for a version the driver brings
        if (major >= 4 && major > best_major) best = i, best_major = major;
    }
    if (best < 0) {
        rt_log("ffx4: upscaler versions for this device: %s; none is FSR 4 (it needs a Radeon RX 6000, 7000 or 9000 card "
               "or a supported Ryzen APU, and AMD's driver 26.10.1 or newer): FSR 3.1 stays", list);
        return 0;
    }
    g_version = ids[best];
    snprintf(g_name, sizeof g_name, "AMD FSR %.24s", names[best]);   // the name is the library's memory: copied
    for (char *p = g_name; *p; p++)
        if (*p == '*') *p = 0;   // the driver's mark
    for (size_t k = strlen(g_name); k && g_name[k - 1] == ' '; k--) g_name[k - 1] = 0;
    rt_log("ffx4: upscaler versions for this device: %s; FSR 4 is %s (id %#llx)", list, g_name, (unsigned long long)g_version);
    return 1;
}

// One context per render and output size, as vk_ffx.cpp keeps them for FSR 3.1; the descriptors a context was made
// from stay with it, as the API asks ("must remain live until ffxDestroyContext").
struct Up {
    ffxContext ctx = nullptr;
    uint32_t w = 0, h = 0, ow = 0, oh = 0;
    uint64_t used = 0;
    ffxCreateContextDescUpscale desc = {};
    ffxCreateBackendVKDesc backend = {};
    ffxOverrideVersion version = {};
    ffxCreateContextDescUpscaleVersion built = {};
} U[6];
uint64_t up_tick;
const uint64_t UP_RECENT = 8;   // contexts used this recently may sit in a command buffer not submitted yet

FfxApiResource image(const VkFfxImage *i, uint32_t state, uint32_t usage) {
    VkImageCreateInfo ci = {};
    ci.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
    ci.imageType = VK_IMAGE_TYPE_2D;
    ci.format = i->format;
    ci.extent = {i->width, i->height, 1};
    ci.mipLevels = 1;
    ci.arrayLayers = 1;
    ci.usage = i->storage ? VK_IMAGE_USAGE_STORAGE_BIT : 0;
    return ffxApiGetResourceVK((void *)i->image, ffxApiGetImageResourceDescriptionVK(i->image, ci, usage), state);
}

Up *context_for(uint32_t w, uint32_t h, uint32_t ow, uint32_t oh) {
    up_tick++;
    for (Up &u : U)
        if (u.ctx && u.w == w && u.h == h && u.ow == ow && u.oh == oh) {
            u.used = up_tick;
            return &u;
        }
    Up *slot = nullptr, *lru = nullptr;
    for (Up &u : U) {
        if (!u.ctx) {
            slot = &u;
            break;
        }
        if (up_tick - u.used >= UP_RECENT && (!lru || u.used < lru->used)) lru = &u;
    }
    const VkCtx *c = vk_ctx();
    if (!slot && lru) {
        vk_queue_lock(), vkDeviceWaitIdle(c->device), vk_queue_unlock();   // its work may still be in flight
        A.destroy(&lru->ctx, nullptr);
        *lru = Up();
        slot = lru;
    }
    if (!slot) return nullptr;
    *slot = Up();
    slot->built.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_UPSCALE_VERSION;
    slot->built.version = FFX_UPSCALER_VERSION;
    slot->version.header.type = FFX_API_DESC_TYPE_OVERRIDE_VERSION;
    slot->version.header.pNext = &slot->built.header;
    slot->version.versionId = g_version;
    slot->backend.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_BACKEND_VK;
    slot->backend.header.pNext = &slot->version.header;
    slot->backend.deviceContext = {c->device, c->phys, vkGetDeviceProcAddr};
    slot->desc.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_UPSCALE;
    slot->desc.header.pNext = &slot->backend.header;
    slot->desc.flags =
        FFX_UPSCALE_ENABLE_HIGH_DYNAMIC_RANGE | FFX_UPSCALE_ENABLE_DEPTH_INFINITE | FFX_UPSCALE_ENABLE_AUTO_EXPOSURE;
    slot->desc.maxRenderSize = {w, h};
    slot->desc.maxUpscaleSize = {ow, oh};
    slot->desc.fpMessage = message;
    uint64_t t0 = plat_now_ns();
    ffxReturnCode_t r = A.create(&slot->ctx, &slot->desc.header, nullptr);
    if (r != FFX_API_RETURN_OK || !slot->ctx) {
        rt_log("ffx4: %s context creation at %ux%u -> %ux%u failed (%u)", g_name, w, h, ow, oh, (unsigned)r);
        *slot = Up();
        return nullptr;
    }
    slot->w = w, slot->h = h, slot->ow = ow, slot->oh = oh, slot->used = up_tick;
    ffxQueryGetProviderVersion v = {};
    v.header.type = FFX_API_QUERY_DESC_TYPE_GET_PROVIDER_VERSION;
    A.query(&slot->ctx, &v.header);
    FfxApiEffectMemoryUsage mem = {};
    ffxQueryDescUpscaleGetGPUMemoryUsage mq = {};
    mq.header.type = FFX_API_QUERY_DESC_TYPE_UPSCALE_GPU_MEMORY_USAGE;
    mq.gpuMemoryUsageUpscaler = &mem;
    A.query(&slot->ctx, &mq.header);
    rt_log("ffx4: FSR %s upscaling at %ux%u -> %ux%u, made in %.0f ms, %.0f MiB of video memory",
           v.versionName ? v.versionName : "4", w, h, ow, oh, (double)(plat_now_ns() - t0) / 1e6,
           (double)mem.totalUsageInBytes / 1048576.0);
    // The provider is asked for the version it runs: anything but a 4.x here means the loader gave another upscaler
    if (v.versionName && atoi(v.versionName) < 4) {
        rt_log("ffx4: the context runs FSR %s, not FSR 4: FSR 4 off", v.versionName);
        A.destroy(&slot->ctx, nullptr);
        *slot = Up();
        return nullptr;
    }
    return slot;
}

void failed(const char *what, unsigned code) {
    if (++g_failures == FAIL_MAX) {
        rt_log("ffx4: %s failed %d times (last %u): FSR 4 is off for this run, FSR 3.1 takes its place", what, FAIL_MAX, code);
        g_state.store(0, std::memory_order_release);
    }
}

}  // namespace

extern "C" int vk_ffx4_available(void) {
    int s = g_state.load(std::memory_order_acquire);
    if (s >= 0) return s;
    std::lock_guard<std::mutex> hold(g_lock);
    s = g_state.load(std::memory_order_acquire);
    if (s >= 0) return s;
    s = probe();
    if (s >= 0) g_state.store(s, std::memory_order_release);
    return s > 0;
}

extern "C" const char *vk_ffx4_name(void) { return g_name; }

extern "C" int vk_ffx4_upscale(VkCommandBuffer cb, const VkFfxImage *color, const VkFfxImage *depth, const VkFfxImage *mv,
                               const VkFfxImage *out, float jx, float jy, int reset, float dt_ms, float fov_y) {
    if (!vk_ffx4_available()) return 0;
    Up *u = context_for(color->width, color->height, out->width, out->height);
    if (!u) {
        failed("making a context", 0);
        return 0;
    }
    ffxDispatchDescUpscale d = {};
    d.header.type = FFX_API_DISPATCH_DESC_TYPE_UPSCALE;
    d.commandList = cb;
    // Every image is in GENERAL, which the back end reads as its unordered-access state, and is left there
    d.color = image(color, FFX_API_RESOURCE_STATE_UNORDERED_ACCESS, 0);
    d.depth = image(depth, FFX_API_RESOURCE_STATE_UNORDERED_ACCESS, 0);
    d.motionVectors = image(mv, FFX_API_RESOURCE_STATE_UNORDERED_ACCESS, 0);
    d.output = image(out, FFX_API_RESOURCE_STATE_UNORDERED_ACCESS, FFX_API_RESOURCE_USAGE_UAV);
    d.jitterOffset = {jx, jy};
    d.motionVectorScale = {1.0f, 1.0f};
    d.renderSize = {color->width, color->height};
    d.upscaleSize = {out->width, out->height};
    int sharp = cfg_int("NX2NAVI_SHARPNESS");   // the slider drives the upscaler's own sharpening, as for FSR 3.1
    d.enableSharpening = sharp > 0;
    d.sharpness = sharp > 100 ? 1.0f : sharp / 100.0f;
    d.frameTimeDelta = dt_ms;
    d.preExposure = 1.0f;
    d.reset = reset != 0;
    d.cameraNear = 0.1f;   // Ocarina's projection (vk_dlaa.c ocam_vp): near 0.1, far at infinity
    d.cameraFar = 1e8f;
    d.cameraFovAngleVertical = fov_y > 0 ? fov_y : 1.0f;
    d.viewSpaceToMetersFactor = 0.01f;   // the game's units are centimetres
    ffxReturnCode_t r = A.dispatch(&u->ctx, &d.header);
    if (r != FFX_API_RETURN_OK) {
        static int told;
        if (told++ < 8) rt_log("ffx4: %s dispatch failed (%u)", g_name, (unsigned)r);
        failed("a dispatch", (unsigned)r);
        return 0;
    }
    return 1;
}

// The loader's calls for vk_fg4.cpp (vk_ffx4_api.h).
extern "C" int vk_ffx4_api_file(const char *name) {
    char path[MAX_PATH];
    return beside_exe(name, path, sizeof path) && file_there(path);
}
extern "C" int vk_ffx4_api_versions(uint64_t create_type, uint64_t *ids, const char **names, int cap) {
    std::lock_guard<std::mutex> hold(g_lock);
    return load() > 0 ? versions(create_type, ids, names, cap) : 0;
}
extern "C" ffxReturnCode_t vk_ffx4_api_create(ffxContext *ctx, ffxCreateContextDescHeader *desc) {
    return A.create ? A.create(ctx, desc, nullptr) : FFX_API_RETURN_ERROR;
}
extern "C" ffxReturnCode_t vk_ffx4_api_destroy(ffxContext *ctx) {
    return A.destroy ? A.destroy(ctx, nullptr) : FFX_API_RETURN_ERROR;
}
extern "C" ffxReturnCode_t vk_ffx4_api_configure(ffxContext *ctx, const ffxConfigureDescHeader *desc) {
    return A.configure ? A.configure(ctx, desc) : FFX_API_RETURN_ERROR;
}
extern "C" ffxReturnCode_t vk_ffx4_api_query(ffxContext *ctx, ffxQueryDescHeader *desc) {
    return A.query ? A.query(ctx, desc) : FFX_API_RETURN_ERROR;
}
extern "C" ffxReturnCode_t vk_ffx4_api_dispatch(ffxContext *ctx, const ffxDispatchDescHeader *desc) {
    return A.dispatch ? A.dispatch(ctx, desc) : FFX_API_RETURN_ERROR;
}

extern "C" void vk_ffx4_release(void) {
    if (!A.destroy) return;
    for (Up &u : U)
        if (u.ctx) A.destroy(&u.ctx, nullptr), u = Up();
}
#endif   // _WIN32
