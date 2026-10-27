type NamedAccount = {
  remark?: string;
  nickname?: string;
  displayName?: string;
  id?: string;
  accountId?: string;
};

/** 账号选择器使用平台名称，并在后面附上用户备注；不让备注覆盖账号名称。 */
export const accountDisplayName = (account?: NamedAccount, fallback = '未命名账号'): string => {
  const name = account?.nickname?.trim() || account?.displayName?.trim();
  const remark = account?.remark?.trim();
  if (!account?.nickname?.trim() && name && remark && name.endsWith(`（${remark}）`)) return name;
  if (name && remark && name !== remark) return `${name}（${remark}）`;
  return name || remark || account?.id || account?.accountId || fallback;
};

export const accountDisplayNameWithMeta = (
  account: NamedAccount | undefined,
  meta: string | number,
  fallback = '未命名账号',
): string => {
  const name = account?.nickname?.trim() || account?.displayName?.trim();
  const remark = account?.remark?.trim();
  const base = name || remark || account?.id || account?.accountId || fallback;
  if (!account?.nickname?.trim() && name && remark && name.endsWith(`（${remark}）`)) return `${name} · ${meta}`;
  if (name && remark && name !== remark) return `${name}（${remark} · ${meta}）`;
  return `${base}（${meta}）`;
};
