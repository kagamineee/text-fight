import type { PublicAttr } from './types'

export function attributeEffects(attr: PublicAttr, turn: number): string {
  const effects: string[] = []
  if (attr.shield) {
    effects.push(attr.lethal_only ? '抵挡致命伤害' : '免疫攻击伤害')
    effects.push(attr.shield_charges > 0 ? `剩余 ${attr.shield_charges} 次` : '不限次数')
  }
  if (attr.freeze) effects.push(`无法行动，剩余 ${attr.charges} 次本人行动`)
  if (attr.damage_reduction > 0) effects.push(`伤害降低 ${attr.damage_reduction}%`)
  if (attr.block_heal_pct > 0) effects.push(`格挡后全体存活玩家回复 ${attr.block_heal_pct}% 最大生命`)
  if (attr.per_turn) effects.push(`每个全局回合${attr.per_turn > 0 ? '回复' : '损失'} ${Math.abs(attr.per_turn)} HP`)
  if (attr.attr_type === 'defense' && !attr.shield && !attr.freeze) effects.push(`护甲 ${attr.value}/${attr.cap}`)
  if (attr.attr_type === 'attack') effects.push(`攻击强度 ${attr.value}/${attr.cap}`)
  if (attr.indestructible) effects.push('不可破坏')
  if (!effects.length) effects.push('无自动结算效果')
  if (attr.expires_turn > 0) effects.push(`剩余 ${Math.max(0, attr.expires_turn - turn + 1)} 个全局回合`)
  return effects.join('；')
}
