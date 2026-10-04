import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { normalizeDomain, normalizeKeyword, normalizeWatchValue } from "../src/lib/watch.js";

describe("normalizeDomain", () => {
  it("reduces every spelling of a domain to one value", () => {
    for (const input of [
      "acme.com",
      "ACME.com",
      "  acme.com  ",
      "https://www.acme.com/login?next=/",
      "http://acme.com:8080/",
      "ops@acme.com",
      "user:pw@www.acme.com/path",
      "acme.com.",
    ]) {
      assert.equal(normalizeDomain(input), "acme.com", input);
    }
  });

  it("keeps subdomains, which are a different question from the apex", () => {
    assert.equal(normalizeDomain("mail.acme.co.uk"), "mail.acme.co.uk");
  });

  it("accepts punycode TLDs", () => {
    assert.equal(normalizeDomain("example.xn--p1ai"), "example.xn--p1ai");
  });

  it("rejects things that are not domains", () => {
    for (const input of ["", "acme", "localhost", "not a domain", "a..com", "-acme.com", "1.2.3.4", "acme.c"]) {
      assert.equal(normalizeDomain(input), null, input);
    }
  });
});

describe("normalizeKeyword", () => {
  it("trims, lowercases and collapses whitespace", () => {
    assert.equal(normalizeKeyword("  Acme   Holdings "), "acme holdings");
  });

  it("rejects keywords too short or too punctuated to mean anything", () => {
    assert.equal(normalizeKeyword("ab"), null);
    assert.equal(normalizeKeyword("---"), null);
    assert.equal(normalizeKeyword("x".repeat(101)), null);
    assert.equal(normalizeKeyword("IBM"), "ibm");
  });

  it("keeps non-latin letters", () => {
    assert.equal(normalizeKeyword("Банк Россия"), "банк россия");
  });
});

describe("normalizeWatchValue", () => {
  it("dispatches on kind", () => {
    assert.equal(normalizeWatchValue("domain", "https://acme.com"), "acme.com");
    assert.equal(normalizeWatchValue("keyword", "https://acme.com"), "https://acme.com");
    assert.equal(normalizeWatchValue("domain", "Acme Holdings"), null);
  });
});
