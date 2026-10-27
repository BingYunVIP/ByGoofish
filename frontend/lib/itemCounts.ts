type ItemListing = { cookie_id: string; listing_status?: string | null };

/** 商品选择器计数只统计当前列表中的在售商品，历史下架记录仍保留在商品库。 */
export const onSaleItemCounts = (items: ItemListing[]): Map<string, number> => {
  const counts = new Map<string, number>();
  for (const item of items) {
    if (item.listing_status !== 'on_sale') continue;
    counts.set(item.cookie_id, (counts.get(item.cookie_id) || 0) + 1);
  }
  return counts;
};
