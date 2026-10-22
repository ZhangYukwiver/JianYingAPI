import { rename } from "node:fs/promises";
import { setTimeout as delay } from "node:timers/promises";

const TRANSIENT = new Set(["EPERM", "EACCES", "EBUSY"]);

/**
 * 「写临时文件再改名替换」在 Windows 上会被短暂占着目标或临时文件的进程挡住（杀毒扫描、索引服务），
 * 报 EPERM/EACCES/EBUSY，过一会儿就好（Windows CI 上实测）。和 graceful-fs 一样短暂重试，最多约 3 秒；
 * 其他平台、其他错误照常抛出。
 */
export async function renameRetrying(from, to, { platform = process.platform, renameImpl = rename, attempts = 20 } = {}) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await renameImpl(from, to);
    } catch (error) {
      if (platform !== "win32" || attempt >= attempts || !TRANSIENT.has(error?.code)) throw error;
      await delay(Math.min(25 * (attempt + 1), 200));
    }
  }
}
