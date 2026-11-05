import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const S = require("../../privatepartynet/web/safetext.js");

const PEM = "-----BEGIN CERTIFICATE-----\nMIIDKDCCAhCgAwIBAgIUFq2LkVc0d0YQ4m1d8Y9e0jYkq3QwDQYJKoZIhvcNAQEL\n-----END CERTIFICATE-----\n";

test("plain messages are shown as they are", () => {
  for (const text of ["Party is open.", "Could not reach the party (timed out).", "",
    "Could not create the certificate: [Errno 13] Permission denied: 'D:\\Games\\Party data\\PrivatePartyNet\\privatepartynet-cert.pem'",
    "Could not check for updates: objects.githubusercontent.com is not a host updates come from"]) {
    assert.equal(S.secretLike(text), "", text);
  }
});

test("a certificate, a key, an invite code or a token is recognised", () => {
  assert.equal(S.secretLike(PEM), "a certificate or key");
  assert.equal(S.secretLike("KeyError: '" + PEM + "'"), "a certificate or key");
  assert.equal(S.secretLike("-----BEGIN PRIVATE KEY-----"), "a certificate or key");
  assert.equal(S.secretLike("the code ppn1-eyJuIjoiUGFydHkiLCJwIjoiMTIzNDU2In0abcdefghij is wrong"), "a code or token");
  assert.equal(S.secretLike("open https://192.168.1.95:8765/join?token=Zx8hQ2kLm9PqRsTuVwXyZ0123456789abcdefABCDEF"),
    "a code or token");
  assert.equal(S.secretLike("88d103cbd9024db9a709a330d229fc7542e482efda81cc3983f4aecbc88cda7c"), "a code or token");
});

test("what a status box shows in its place says where to look", () => {
  assert.equal(S.shown("Party is open."), "Party is open.");
  assert.match(S.shown(PEM), /^A message was hidden because it looked like a certificate or key/);
  assert.equal(S.shown(null), "");
  assert.equal(S.shown(undefined), "");
});
