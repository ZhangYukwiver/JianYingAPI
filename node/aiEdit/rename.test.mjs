import { describe, expect, it } from "vitest";

import { renameRetrying } from "./rename.mjs";

const failing = (codes) => {
  const calls = [];
  const renameImpl = async (from, to) => {
    calls.push([from, to]);
    const code = codes.shift();
    if (code) throw Object.assign(new Error(code), { code });
  };
  return { calls, renameImpl };
};

describe("renameRetrying", () => {
  it("retries a briefly locked target on Windows until the rename goes through", async () => {
    const { calls, renameImpl } = failing(["EPERM", "EBUSY", "EACCES"]);
    await renameRetrying("a.tmp", "a", { platform: "win32", renameImpl });
    expect(calls).toHaveLength(4);
  });

  it("gives up after the last attempt and rethrows", async () => {
    const { calls, renameImpl } = failing(["EPERM", "EPERM", "EPERM"]);
    await expect(renameRetrying("a.tmp", "a", { platform: "win32", renameImpl, attempts: 2 })).rejects.toMatchObject({ code: "EPERM" });
    expect(calls).toHaveLength(3);
  });

  it("does not retry on other platforms or for other errors", async () => {
    const mac = failing(["EPERM"]);
    await expect(renameRetrying("a.tmp", "a", { platform: "darwin", renameImpl: mac.renameImpl })).rejects.toMatchObject({ code: "EPERM" });
    expect(mac.calls).toHaveLength(1);
    const missing = failing(["ENOENT"]);
    await expect(renameRetrying("a.tmp", "a", { platform: "win32", renameImpl: missing.renameImpl })).rejects.toMatchObject({ code: "ENOENT" });
    expect(missing.calls).toHaveLength(1);
  });
});
