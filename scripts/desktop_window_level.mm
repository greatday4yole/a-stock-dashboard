#import <Cocoa/Cocoa.h>
#import <CoreGraphics/CoreGraphics.h>
#include <node_api.h>
#include <cstring>

namespace {

NSWindow* WindowFromHandle(napi_env env, napi_value handle) {
  bool isBuffer = false;
  if (napi_is_buffer(env, handle, &isBuffer) != napi_ok || !isBuffer) {
    napi_throw_type_error(env, nullptr, "窗口句柄必须是 Buffer");
    return nil;
  }
  void* data = nullptr;
  size_t length = 0;
  if (napi_get_buffer_info(env, handle, &data, &length) != napi_ok
      || data == nullptr || length != sizeof(NSView*)) {
    napi_throw_type_error(env, nullptr, "无效的 macOS 窗口句柄");
    return nil;
  }
  __unsafe_unretained NSView* view = nullptr;
  std::memcpy(&view, data, sizeof(view));
  NSWindow* window = view.window;
  if (window == nil) napi_throw_error(env, nullptr, "找不到组件对应的 NSWindow");
  return window;
}

napi_value SetDesktopMode(napi_env env, napi_callback_info info) {
  size_t argc = 2;
  napi_value args[2];
  if (napi_get_cb_info(env, info, &argc, args, nullptr, nullptr) != napi_ok || argc != 2) {
    napi_throw_type_error(env, nullptr, "setDesktopMode 需要窗口句柄和布尔值");
    return nullptr;
  }
  bool enabled = false;
  if (napi_get_value_bool(env, args[1], &enabled) != napi_ok) {
    napi_throw_type_error(env, nullptr, "桌面吸附开关必须是布尔值");
    return nullptr;
  }
  NSWindow* window = WindowFromHandle(env, args[0]);
  if (window == nil) return nullptr;

  if (enabled) {
    // 位于桌面图标之上、普通应用窗口之下，确保桌面层仍可接收鼠标事件。
    const NSInteger desktopLevel = static_cast<NSInteger>(CGWindowLevelForKey(kCGDesktopIconWindowLevelKey)) + 1;
    window.movable = true;
    window.level = desktopLevel;
    window.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces
      | NSWindowCollectionBehaviorStationary
      | NSWindowCollectionBehaviorIgnoresCycle;
    window.hidesOnDeactivate = false;
    [window orderFrontRegardless];
  } else {
    window.level = NSNormalWindowLevel;
    window.collectionBehavior = NSWindowCollectionBehaviorDefault;
  }
  return nullptr;
}

napi_value SetFloatingMode(napi_env env, napi_callback_info info) {
  size_t argc = 2;
  napi_value args[2];
  if (napi_get_cb_info(env, info, &argc, args, nullptr, nullptr) != napi_ok || argc != 2) {
    napi_throw_type_error(env, nullptr, "setFloatingMode 需要窗口句柄和布尔值");
    return nullptr;
  }
  bool enabled = false;
  if (napi_get_value_bool(env, args[1], &enabled) != napi_ok) {
    napi_throw_type_error(env, nullptr, "浮动层级开关必须是布尔值");
    return nullptr;
  }
  NSWindow* window = WindowFromHandle(env, args[0]);
  if (window == nil) return nullptr;

  if (enabled) {
    // 交易时段悬浮在普通应用窗口之上，跨桌面空间并允许覆盖全屏应用。
    const NSInteger floatingLevel = static_cast<NSInteger>(CGWindowLevelForKey(kCGFloatingWindowLevelKey));
    window.movable = true;
    window.level = floatingLevel;
    window.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces
      | NSWindowCollectionBehaviorStationary
      | NSWindowCollectionBehaviorIgnoresCycle
      | NSWindowCollectionBehaviorFullScreenAuxiliary;
    window.hidesOnDeactivate = false;
    [window orderFrontRegardless];
  } else {
    window.level = NSNormalWindowLevel;
    window.collectionBehavior = NSWindowCollectionBehaviorDefault;
  }
  return nullptr;
}

napi_value GetWindowLevel(napi_env env, napi_callback_info info) {
  size_t argc = 1;
  napi_value args[1];
  if (napi_get_cb_info(env, info, &argc, args, nullptr, nullptr) != napi_ok || argc != 1) {
    napi_throw_type_error(env, nullptr, "getWindowLevel 需要窗口句柄");
    return nullptr;
  }
  NSWindow* window = WindowFromHandle(env, args[0]);
  if (window == nil) return nullptr;
  napi_value result;
  if (napi_create_double(env, static_cast<double>(window.level), &result) != napi_ok) return nullptr;
  return result;
}

NAPI_MODULE_INIT() {
  napi_value setDesktopMode;
  napi_value setFloatingMode;
  napi_value getWindowLevel;
  if (napi_create_function(env, "setDesktopMode", NAPI_AUTO_LENGTH, SetDesktopMode, nullptr, &setDesktopMode) != napi_ok
      || napi_create_function(env, "setFloatingMode", NAPI_AUTO_LENGTH, SetFloatingMode, nullptr, &setFloatingMode) != napi_ok
      || napi_create_function(env, "getWindowLevel", NAPI_AUTO_LENGTH, GetWindowLevel, nullptr, &getWindowLevel) != napi_ok
      || napi_set_named_property(env, exports, "setDesktopMode", setDesktopMode) != napi_ok
      || napi_set_named_property(env, exports, "setFloatingMode", setFloatingMode) != napi_ok
      || napi_set_named_property(env, exports, "getWindowLevel", getWindowLevel) != napi_ok) {
    return nullptr;
  }
  return exports;
}

}  // namespace
