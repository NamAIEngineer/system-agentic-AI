import assert from "node:assert/strict";
import test from "node:test";
import { isPublicAddress, resolvePinned } from "./local-agent-egress-proxy.mjs";

test("public-address policy rejects private, special-use, and mapped-private addresses", () => {
  for (const address of ["127.0.0.1", "10.1.2.3", "169.254.1.2", "100.64.0.1", "192.0.2.1", "::1", "fc00::1", "fe80::1", "::ffff:192.168.1.1", "2001:db8::1", "2002::1", "3fff::1"]) {
    assert.equal(isPublicAddress(address), false, address);
  }
  for (const address of ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111", "2001:4860:4860::8888"]) {
    assert.equal(isPublicAddress(address), true, address);
  }
});

test("mixed public and private DNS answers fail closed", async () => {
  await assert.rejects(
    resolvePinned("mixed.example", async () => [
      { address: "93.184.216.34", family: 4 },
      { address: "127.0.0.1", family: 4 },
    ]),
    /non-public or mixed DNS answers blocked/,
  );
});

test("a later rebinding answer is independently rejected before connect", async () => {
  const answers = [
    [{ address: "93.184.216.34", family: 4 }],
    [{ address: "127.0.0.1", family: 4 }],
  ];
  const resolver = async () => answers.shift();
  const pinned = await resolvePinned("rebind.example", resolver);
  assert.deepEqual(pinned, { address: "93.184.216.34", family: 4 });
  await assert.rejects(resolvePinned("rebind.example", resolver), /non-public or mixed DNS answers blocked/);
});
