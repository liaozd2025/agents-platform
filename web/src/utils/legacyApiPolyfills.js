/**
 * 老内核缺失的 ES2022 / ES2023 方法兜底。
 *
 * 背景：终端存在 Chrome 86（内网没有 VPN，装不了新版浏览器），而项目与第三方依赖会用到
 * `Array.prototype.at`（92+）、`findLast` / `findLastIndex`（97+）、`Object.hasOwn`（93+）、
 * `String.prototype.at`（92+）等方法，缺失时不是「少个功能」而是**直接抛 TypeError 让页面白屏**。
 *
 * 为什么做成入口处的兜底、而不是逐个改调用点：
 * 1. 这些方法会被第三方依赖内部使用，改项目源码根本覆盖不到；
 * 2. 一处兜底以后，新写的代码再用这些方法也不会退化成白屏，避免同类问题反复出现。
 *
 * 全部用「原生不存在才定义」的守卫式写法：现代浏览器走的仍然是原生实现，行为零变化。
 * 只补真的会让页面崩掉的方法，缺了也「不致命」的新 API（如 toSorted / Object.groupBy）不补，保持最小。
 */

// Array.prototype.at（Chrome 92+）：支持负索引，越界返回 undefined
if (!Array.prototype.at) {
  Object.defineProperty(Array.prototype, 'at', {
    value: function at(index) {
      const length = this.length >>> 0
      // Number(index) || 0 与原生一致：把 undefined / NaN 都当成 0
      const position = Number(index) || 0
      return position < 0 ? this[length + position] : this[position]
    },
    writable: true,
    configurable: true
  })
}

// String.prototype.at（Chrome 92+）：按 UTF-16 单元取字符，与原生一致
if (!String.prototype.at) {
  Object.defineProperty(String.prototype, 'at', {
    value: function at(index) {
      const value = String(this)
      const position = Number(index) || 0
      return position < 0 ? value[value.length + position] : value[position]
    },
    writable: true,
    configurable: true
  })
}

// Array.prototype.findLast（Chrome 97+）：从后往前找到第一个满足条件的元素
if (!Array.prototype.findLast) {
  Object.defineProperty(Array.prototype, 'findLast', {
    value: function findLast(predicate, thisArg) {
      if (typeof predicate !== 'function') {
        throw new TypeError('predicate must be a function')
      }
      // 必须倒序遍历：这是 findLast 与 find 的语义差别所在
      for (let index = this.length - 1; index >= 0; index -= 1) {
        if (predicate.call(thisArg, this[index], index, this)) return this[index]
      }
      return undefined
    },
    writable: true,
    configurable: true
  })
}

// Array.prototype.findLastIndex（Chrome 97+）：同上，返回下标，找不到返回 -1
if (!Array.prototype.findLastIndex) {
  Object.defineProperty(Array.prototype, 'findLastIndex', {
    value: function findLastIndex(predicate, thisArg) {
      if (typeof predicate !== 'function') {
        throw new TypeError('predicate must be a function')
      }
      for (let index = this.length - 1; index >= 0; index -= 1) {
        if (predicate.call(thisArg, this[index], index, this)) return index
      }
      return -1
    },
    writable: true,
    configurable: true
  })
}

// Object.hasOwn（Chrome 93+）：等价于 Object.prototype.hasOwnProperty.call
if (!Object.hasOwn) {
  Object.defineProperty(Object, 'hasOwn', {
    value: function hasOwn(target, property) {
      return Object.prototype.hasOwnProperty.call(target, property)
    },
    writable: true,
    configurable: true
  })
}

// Element.prototype.replaceChildren（Chrome 86+）：清空并插入新子节点。
// 项目在 MessageInputComponent.vue:319 用它替换编辑器内容，85 及以下会抛
// TypeError: editor.replaceChildren is not a function（实测 Chromium 85 复现）。
if (typeof Element !== 'undefined' && typeof Element.prototype.replaceChildren !== 'function') {
  Object.defineProperty(Element.prototype, 'replaceChildren', {
    value: function replaceChildren() {
      while (this.firstChild) this.removeChild(this.firstChild)
      for (let index = 0; index < arguments.length; index += 1) {
        const node = arguments[index]
        // 非节点参数按文本节点插入，与原生行为一致
        this.appendChild(node instanceof Node ? node : document.createTextNode(String(node)))
      }
    },
    writable: true,
    configurable: true
  })
}

// String.prototype.replaceAll（Chrome 85+）：项目里多处用它做 HTML 转义、路径处理，
// 缺失时不只会少个功能，而是直接抛 TypeError，低版本上页面会二次报错。
if (!String.prototype.replaceAll) {
  Object.defineProperty(String.prototype, 'replaceAll', {
    value: function replaceAll(searchValue, replaceValue) {
      const source = String(this)
      if (searchValue instanceof RegExp) {
        // 与原生语义一致：正则必须带 g 标志，否则抛 TypeError
        if (!searchValue.global) {
          throw new TypeError('replaceAll must be called with a global RegExp')
        }
        return source.replace(searchValue, replaceValue)
      }
      const needle = String(searchValue)
      // 空搜索串的原生语义：在每个字符之间（含首尾）都插入替换内容
      if (needle === '') return source.replace(/(?:)/g, replaceValue)
      // 字符串搜索必须按字面量处理：直接 new RegExp 会把搜索串里的 . * ( ) 等当正则元字符，
      // 所以先转义再构造全局正则 —— 这样字符串替换器与函数替换器都能正确工作。
      const escaped = needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
      return source.replace(new RegExp(escaped, 'g'), replaceValue)
    },
    writable: true,
    configurable: true
  })
}

/*
 * AbortSignal 一族新 API（老内核集体缺失，实测在 Chromium 85/86 上逐个报错）：
 *   - AbortSignal.any            Chrome 116+  ← src/stores/user.js:154 直接用
 *   - AbortSignal.timeout        Chrome 103+
 *   - AbortSignal.abort          Chrome 93+
 *   - AbortSignal#throwIfAborted  Chrome 100+  ← axios 内部在取消请求时调用
 * 影响：缺失时不是少个能力，而是直接 TypeError → 「获取用户信息失败」→ 登录态建不起来 → 被踢回登录页，
 * 让人误以为「登录坏了」。AbortSignal / AbortController 本体从 Chrome 66 起就有，老内核都在。
 * 注意逐个补是行不通的（修一个冒一个），所以整族一起补。
 */
if (typeof AbortSignal !== 'undefined') {
  if (typeof AbortSignal.any !== 'function') {
    Object.defineProperty(AbortSignal, 'any', {
      value: function any(signals) {
        const controller = new AbortController()
        for (const signal of Array.from(signals)) {
          // 已中断的直接短路，与原生语义一致
          if (signal.aborted) {
            controller.abort(signal.reason)
            break
          }
          signal.addEventListener('abort', () => controller.abort(signal.reason), { once: true })
        }
        return controller.signal
      },
      writable: true,
      configurable: true
    })
  }

  if (typeof AbortSignal.abort !== 'function') {
    Object.defineProperty(AbortSignal, 'abort', {
      value: function abort(reason) {
        const controller = new AbortController()
        controller.abort(reason)
        return controller.signal
      },
      writable: true,
      configurable: true
    })
  }

  if (typeof AbortSignal.timeout !== 'function') {
    Object.defineProperty(AbortSignal, 'timeout', {
      value: function timeout(milliseconds) {
        const controller = new AbortController()
        setTimeout(function () {
          controller.abort(new DOMException('The operation timed out.', 'TimeoutError'))
        }, Number(milliseconds) || 0)
        return controller.signal
      },
      writable: true,
      configurable: true
    })
  }

  if (typeof AbortSignal.prototype.throwIfAborted !== 'function') {
    Object.defineProperty(AbortSignal.prototype, 'throwIfAborted', {
      value: function throwIfAborted() {
        if (!this.aborted) return
        // 老内核的 abort() 不接受 reason 参数，this.reason 常常是 undefined，
        // 因此这里兜一个标准的 AbortError，保证调用方能拿到可识别的异常对象。
        throw this.reason !== undefined
          ? this.reason
          : new DOMException('This operation was aborted', 'AbortError')
      },
      writable: true,
      configurable: true
    })
  }
}
