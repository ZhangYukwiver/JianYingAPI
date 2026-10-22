// AI 剪辑沿用 JianYingAPI 的错误约定：英文错误码 + 说明 + HTTP 状态 + details。
// 码和状态与原仓库 errors.py 一致（ServiceError 默认 400，ValidationFailure 是 422），
// 这样移植后的行为能和原仓库逐条对照；页面上要给用户看的话由路由层另配中文。

export class EditError extends Error {
  constructor(code, message, { status = 400, details = {} } = {}) {
    super(message);
    this.name = "EditError";
    this.code = code;
    this.status = status;
    this.details = details;
  }

  toJSON() {
    return { code: this.code, message: this.message, details: this.details };
  }
}

/** 对应原仓库的 ValidationFailure：状态固定 422，其余参数进 details。 */
export function invalid(code, message, details = {}) {
  return new EditError(code, message, { status: 422, details });
}
