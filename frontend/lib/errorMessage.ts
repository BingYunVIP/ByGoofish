interface ApiErrorLike {
  response?: { data?: { detail?: unknown } };
  message?: string;
}

/** 取后端返回的可读错误说明；没有时退回异常自带的信息，再退回 fallback。 */
export const describeApiError = (error: unknown, fallback = '操作失败，请稍后重试'): string => {
  const detail = (error as ApiErrorLike | undefined)?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  if (Array.isArray(detail)) {
    // FastAPI 参数校验错误：[{ loc, msg }]
    const first = detail[0] as { msg?: unknown } | undefined;
    if (typeof first?.msg === 'string' && first.msg.trim()) return first.msg.trim();
  } else if (detail && typeof detail === 'object') {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === 'string' && message.trim()) return message.trim();
  }
  const message = (error as ApiErrorLike | undefined)?.message;
  return typeof message === 'string' && message.trim() ? message.trim() : fallback;
};
