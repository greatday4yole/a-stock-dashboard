/* Static-deployment adapter for the A-share dashboard.
 *
 * The original dashboard talks to a loopback Python server.  This script
 * intercepts /api/* requests, loads AES-GCM encrypted JSON snapshots that were
 * produced by the scheduled GitHub Actions export, and returns them through a
 * normal fetch Response.  Data stays encrypted at rest on GitHub Pages and is
 * only decrypted in the browser after the visitor enters the access password.
 */
(function () {
  "use strict";

  var script = document.currentScript;
  var BASE = script && script.src
    ? new URL(".", script.src).href
    : new URL("./", window.location.href).href;

  var META_URL = BASE + "crypto-meta.json";
  var VERIFY_KEY = "verify";
  var STORAGE_KEY = "asd-static-key-v1";
  var MANIFEST_URL = BASE + "static-api/manifest.json";
  function withCacheBust(url) {
    return url + (url.indexOf("?") >= 0 ? "&" : "?") + "v=" + Date.now();
  }

  var nativeFetch = window.fetch.bind(window);
  var textEncoder = new TextEncoder();
  var textDecoder = new TextDecoder();

  var unlocked = false;
  var releaseUnlock;
  var unlockedPromise = new Promise(function (resolve) { releaseUnlock = resolve; });
  var cryptoKeyPromise = null;
  var metaPromise = null;
  var manifestPromise = null;
  var jsonCache = new Map();

  function base64ToBytes(value) {
    var binary = window.atob(value);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }

  function bytesToBase64(bytes) {
    var binary = "";
    for (var i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
    return window.btoa(binary);
  }

  function jsonResponse(payload, status) {
    return new Response(JSON.stringify(payload), {
      status: status || 200,
      headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
    });
  }

  function getMeta() {
    if (!metaPromise) {
      metaPromise = nativeFetch(withCacheBust(META_URL), { cache: "no-store" }).then(function (response) {
        if (!response.ok) throw new Error("无法读取加密元数据 HTTP " + response.status);
        return response.json();
      });
    }
    return metaPromise;
  }

  function deriveKey(password, meta) {
    if (!window.crypto || !window.crypto.subtle) {
      return Promise.reject(new Error("当前浏览器不支持 WebCrypto，请通过 HTTPS 访问"));
    }
    return window.crypto.subtle
      .importKey("raw", textEncoder.encode(password), "PBKDF2", false, ["deriveKey"])
      .then(function (baseKey) {
        return window.crypto.subtle.deriveKey(
          {
            name: "PBKDF2",
            salt: base64ToBytes(meta.salt),
            iterations: Number(meta.iterations) || 210000,
            hash: "SHA-256",
          },
          baseKey,
          { name: "AES-GCM", length: 256 },
          true,
          ["decrypt"]
        );
      });
  }

  function decryptWithKey(key, url) {
    return nativeFetch(url, { cache: "no-store" }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.arrayBuffer();
    }).then(function (buffer) {
      if (buffer.byteLength <= 12) throw new Error("加密数据不完整");
      var iv = new Uint8Array(buffer, 0, 12);
      var ciphertext = new Uint8Array(buffer, 12);
      return window.crypto.subtle.decrypt({ name: "AES-GCM", iv: iv }, key, ciphertext);
    }).then(function (plain) {
      return JSON.parse(textDecoder.decode(plain));
    });
  }

  function finishUnlock(key, remember) {
    cryptoKeyPromise = Promise.resolve(key);
    unlocked = true;
    document.documentElement.classList.remove("asd-gate-open");
    if (remember) {
      try {
        window.crypto.subtle.exportKey("raw", key).then(function (raw) {
          try { window.localStorage.setItem(STORAGE_KEY, bytesToBase64(new Uint8Array(raw))); } catch (_) {}
        });
      } catch (_) {}
    }
    releaseUnlock();
    removeGate();
    window.scrollTo(0, 0);
    document.documentElement.scrollTop = 0;
    document.body.scrollTop = 0;
  }

  function trySavedKey() {
    var saved;
    try { saved = window.localStorage.getItem(STORAGE_KEY); } catch (_) { saved = null; }
    if (!saved) return Promise.resolve(false);
    var key;
    try {
      key = window.crypto.subtle.importKey(
        "raw", base64ToBytes(saved), { name: "AES-GCM" }, true, ["decrypt"]
      );
    } catch (_) {
      return Promise.resolve(false);
    }
    return key.then(function (cryptoKey) {
      return decryptWithKey(cryptoKey, withCacheBust(BASE + "static-api/" + VERIFY_KEY + ".json.enc"))
        .then(function (verify) {
          if (!verify || verify.ok !== true) return false;
          finishUnlock(cryptoKey, false);
          return true;
        });
    }).catch(function () {
      try { window.localStorage.removeItem(STORAGE_KEY); } catch (_) {}
      return false;
    });
  }

  function getManifest() {
    if (!manifestPromise) {
      manifestPromise = nativeFetch(withCacheBust(MANIFEST_URL), { cache: "no-store" })
        .then(function (response) { return response.ok ? response.json() : { keys: [] }; })
        .catch(function () { return { keys: [] }; });
    }
    return manifestPromise;
  }

  function apiKey(url) {
    var apiIndex = url.pathname.indexOf("/api/");
    if (apiIndex < 0) return null;
    var endpoint = url.pathname.slice(apiIndex + 5);
    var params = url.searchParams;
    var portfolio = params.get("portfolio") || "";
    var suffix = portfolio ? "--" + portfolio : "";

    switch (endpoint) {
      case "quotes": return "quotes" + suffix;
      case "portfolios": return "portfolios";
      case "personal-index": return "personal-index";
      case "daily-pnl-history": return "daily-pnl-history" + suffix;
      case "risk": return "risk" + suffix;
      case "news": return "news" + suffix;
      case "market-news": return "market-news";
      case "decision-context": return "decision-context";
      case "market-state": return "market-state";
      case "events": return "events";
      case "opportunities": return "opportunities";
      case "rebalance": return "rebalance";
      case "school-signals": return "school-signals";
      case "intraday-pnl": return "intraday-pnl" + suffix;
      case "intraday-pnl/dates": return "intraday-pnl-dates" + suffix;
      case "intraday-pnl/history": {
        var date = params.get("date") || "";
        return "intraday-pnl-history" + suffix + "--" + date;
      }
      case "leading-indicators": return "leading-indicators";
      case "derivatives-regime": return "derivatives-regime";
      case "tomorrow-plans": return "tomorrow-plans";
      case "portfolio-compare": return "portfolio-compare";
      case "paper-t": return "paper-t" + suffix;
      case "paper-t/history": return "paper-t-history" + suffix;
      case "autopilot": return "autopilot";
      case "autopilot/journal": return "autopilot-journal";
      case "health": return "health";
      case "stock-chart": {
        var symbol = params.get("symbol") || "";
        return "stock-chart--" + symbol + (params.get("indicators") ? "--indicators" : "");
      }
      case "technical-signals": return "technical-signals--" + (params.get("symbol") || "");
      case "technical-signals-batch": return "technical-signals-batch";
      default: return null;
    }
  }

  function loadJson(key) {
    if (jsonCache.has(key)) return Promise.resolve(jsonCache.get(key));
    return getManifest().then(function (manifest) {
      if (manifest && Array.isArray(manifest.keys) && manifest.keys.indexOf(key) < 0) {
        return {
          ok: false,
          error_code: "STATIC_DATA_MISSING",
          error: "该数据尚未在静态部署中生成（定时任务会补齐）",
        };
      }
      return cryptoKeyPromise.then(function (cryptoKey) {
        return decryptWithKey(cryptoKey, withCacheBust(BASE + "static-api/" + key + ".json.enc"));
      }).then(function (payload) {
        jsonCache.set(key, payload);
        return payload;
      });
    });
  }

  var nativeFetchWrapper = function (input, init) {
    var rawUrl = typeof input === "string"
      ? input
      : (input && input.url) ? input.url : String(input);
    var url;
    try { url = new URL(rawUrl, window.location.href); } catch (_) { return nativeFetch(input, init); }
    if (url.pathname.indexOf("/api/") < 0) return nativeFetch(input, init);

    var method = String((init && init.method) || (input && input.method) || "GET").toUpperCase();
    if (method !== "GET") {
      return Promise.resolve(jsonResponse({
        ok: false,
        error_code: "READ_ONLY_STATIC_DEPLOY",
        error: "这是只读静态部署，不支持修改组合、回填或作废操作",
      }, 403));
    }

    var key = apiKey(url);
    if (!key) {
      return Promise.resolve(jsonResponse({
        ok: false,
        error_code: "STATIC_UNSUPPORTED_ENDPOINT",
        error: "静态部署不支持该接口",
      }, 404));
    }

    return unlockedPromise.then(function () { return loadJson(key); }).then(function (payload) {
      return jsonResponse(payload, 200);
    }).catch(function (error) {
      return jsonResponse({
        ok: false,
        error_code: "STATIC_DECRYPT_FAILED",
        error: "读取加密数据失败：" + (error && error.message ? error.message : String(error)),
      }, 200);
    });
  };

  // Replace fetch before app.js/autopilot.js run.
  window.fetch = nativeFetchWrapper;

  // ---- password gate ------------------------------------------------------

  function gateStyles() {
    var style = document.createElement("style");
    style.textContent = [
      "#asd-static-gate{position:fixed;inset:0;z-index:2147483647;display:flex;align-items:center;justify-content:center;overflow-y:auto;-webkit-overflow-scrolling:touch;padding:20px;box-sizing:border-box;background:rgba(15,23,42,.82);backdrop-filter:blur(6px);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;}",
      "html.asd-gate-open,html.asd-gate-open body{overflow:hidden;}",
      "#asd-static-gate .asd-card{margin:auto;}",
      "#asd-static-gate .asd-card{width:min(420px,calc(100vw - 40px));background:#fff;border-radius:16px;padding:28px 28px 22px;box-shadow:0 24px 80px rgba(0,0,0,.35);color:#0f172a;}",
      "#asd-static-gate h2{margin:0 0 8px;font-size:20px;line-height:1.3;}",
      "#asd-static-gate p{margin:0 0 16px;color:#475569;font-size:13px;line-height:1.6;}",
      "#asd-static-gate input[type=password]{width:100%;box-sizing:border-box;padding:12px 14px;border:1px solid #cbd5e1;border-radius:10px;font-size:15px;outline:none;}",
      "#asd-static-gate input[type=password]:focus{border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.15);}",
      "#asd-static-gate .asd-row{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:12px 0 18px;font-size:12px;color:#475569;}",
      "#asd-static-gate button{width:100%;padding:12px 16px;border:0;border-radius:10px;background:#2563eb;color:#fff;font-size:15px;font-weight:600;cursor:pointer;}",
      "#asd-static-gate button:disabled{opacity:.6;cursor:wait;}",
      "#asd-static-gate .asd-error{min-height:18px;margin:12px 0 0!important;color:#dc2626!important;font-size:12px!important;}",
      "#asd-static-gate .asd-hint{margin-top:14px!important;color:#94a3b8!important;font-size:11px!important;}",
    ].join("\n");
    document.head.appendChild(style);
  }

  function removeGate() {
    var gate = document.getElementById("asd-static-gate");
    if (gate) gate.remove();
  }

  function positionGate(gate) {
    var vv = window.visualViewport;
    if (!vv) return;
    gate.style.left = vv.offsetLeft + "px";
    gate.style.top = vv.offsetTop + "px";
    gate.style.right = "auto";
    gate.style.bottom = "auto";
    gate.style.width = vv.width + "px";
    gate.style.height = vv.height + "px";
  }

  function watchViewport(gate) {
    var reposition = function () {
      if (gate.isConnected) positionGate(gate);
    };
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", reposition);
      window.visualViewport.addEventListener("scroll", reposition);
    }
    window.addEventListener("resize", reposition);
  }

  function showGate() {
    if (document.getElementById("asd-static-gate")) return;
    gateStyles();
    document.documentElement.classList.add("asd-gate-open");
    var gate = document.createElement("div");
    gate.id = "asd-static-gate";
    gate.setAttribute("role", "dialog");
    gate.setAttribute("aria-modal", "true");
    gate.innerHTML = [
      '<form class="asd-card" id="asd-static-gate-form" autocomplete="on">',
      "<h2>持仓行情 · 访问验证</h2>",
      "<p>该站点包含个人持仓与盈亏数据。请输入部署者设置的访问密码；密码只在本机浏览器中用于解密，不会上传。</p>",
      '<input id="asd-static-gate-password" type="password" required autocomplete="current-password" placeholder="访问密码">',
      '<div class="asd-row"><label><input id="asd-static-gate-remember" type="checkbox" checked> 记住此设备</label><span>HTTPS 加密传输</span></div>',
      '<button id="asd-static-gate-submit" type="submit">进入</button>',
      '<p class="asd-error" id="asd-static-gate-error" role="alert"></p>',
      '<p class="asd-hint">数据由 GitHub Actions 定时刷新；页面为只读快照，不支持修改组合。</p>',
      "</form>",
    ].join("");
    document.documentElement.appendChild(gate);
    positionGate(gate);
    watchViewport(gate);

    var form = document.getElementById("asd-static-gate-form");
    var input = document.getElementById("asd-static-gate-password");
    var submit = document.getElementById("asd-static-gate-submit");
    var errorBox = document.getElementById("asd-static-gate-error");
    setTimeout(function () {
      var coarse = window.matchMedia && window.matchMedia("(pointer: coarse)").matches;
      if (!coarse) input.focus();
    }, 50);

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var password = input.value;
      if (!password) return;
      submit.disabled = true;
      submit.textContent = "验证中…";
      errorBox.textContent = "";
      getMeta()
        .then(function (meta) { return deriveKey(password, meta); })
        .then(function (key) {
          return decryptWithKey(key, withCacheBust(BASE + "static-api/" + VERIFY_KEY + ".json.enc")).then(function (verify) {
            if (!verify || verify.ok !== true) throw new Error("密码不正确");
            finishUnlock(key, document.getElementById("asd-static-gate-remember").checked);
          });
        })
        .catch(function (error) {
          errorBox.textContent = (error && error.message) ? error.message : "密码不正确或数据不可用";
          submit.disabled = false;
          submit.textContent = "进入";
          input.select();
        });
    });
  }

  function bootGate() {
    if (!window.crypto || !window.crypto.subtle) {
      showGate();
      var errorBox = document.getElementById("asd-static-gate-error");
      if (errorBox) errorBox.textContent = "当前环境不支持 WebCrypto，请使用 HTTPS 地址访问。";
      return;
    }
    trySavedKey().then(function (ok) {
      if (!ok) showGate();
    }).catch(function () { showGate(); });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootGate);
  } else {
    bootGate();
  }
})();
