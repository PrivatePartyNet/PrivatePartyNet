/*
 * Keeps certificates, keys, invite codes and tokens out of the control panel's status and error boxes. A message that
 * looks like one is not shown; the box says one was hidden, and the app logs where (never what). Pure functions, so
 * the tests run them in Node.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.PrivatePartyNetSafeText = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var PEM = /-----BEGIN [A-Z0-9 ]+-----|PRIVATE KEY/;
  // A run of 40 or more letters, digits and base64 signs with no space or dot: an invite code (ppn1-...), a join
  // token, a fingerprint or a key. No sentence, file path or host name has one.
  var TOKEN = /[A-Za-z0-9+\/=_-]{40,}/;

  // What `text` looks like, in words ("a certificate or key", "a code or token"), or "" when it is a plain message.
  function secretLike(text) {
    var value = String(text || "");
    if (PEM.test(value)) return "a certificate or key";
    if (TOKEN.test(value)) return "a code or token";
    return "";
  }

  // The text a box may show for `text`.
  function shown(text) {
    var kind = secretLike(text);
    if (!kind) return text == null ? "" : String(text);
    return "A message was hidden because it looked like " + kind + ". The log says where it came from.";
  }

  return { secretLike: secretLike, shown: shown };
});
