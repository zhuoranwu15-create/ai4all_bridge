(function(global) {
  'use strict';

  var state = { language: 'zh-CN', messages: {} };

  function interpolate(template, params) {
    return String(template == null ? '' : template).replace(/\{([a-zA-Z0-9_]+)\}/g, function(_, key) {
      return params && Object.prototype.hasOwnProperty.call(params, key) ? String(params[key]) : '{' + key + '}';
    });
  }

  function t(key, fallback, params) {
    var value = state.messages[key];
    return interpolate(value == null ? (fallback == null ? key : fallback) : value, params || {});
  }

  function apply(root) {
    var scope = root || document;
    scope.querySelectorAll('[data-i18n]').forEach(function(element) {
      element.textContent = t(element.getAttribute('data-i18n'), element.textContent);
    });
    scope.querySelectorAll('[data-i18n-placeholder]').forEach(function(element) {
      element.setAttribute('placeholder', t(element.getAttribute('data-i18n-placeholder'), element.getAttribute('placeholder')));
    });
    scope.querySelectorAll('[data-i18n-alt]').forEach(function(element) {
      element.setAttribute('alt', t(element.getAttribute('data-i18n-alt'), element.getAttribute('alt')));
    });
  }

  function setConfig(config) {
    var product = (config && config.product) || {};
    state.language = product.default_language || 'zh-CN';
    state.messages = (config && config.messages) || {};
    document.documentElement.lang = state.language;
  }

  global.CXProductI18n = { apply: apply, setConfig: setConfig, t: t };
})(window);
