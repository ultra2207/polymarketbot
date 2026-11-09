import json
from polymarketbot.logs import get_logger
log = get_logger(__name__)

def main():
    data = json.load(open('data/processed/live_reward_head_pools.json', encoding='utf-8'))
    log.info(f"{'Market':<38} | {'Daily Rate':<10} | {'Spread':<8} | {'Max Spr':<8} | {'Qualifying $':<14} | {'Touch $'}")
    log.info("-" * 96)
    for m in data:
        q = m['question'][:36]
        d = f"${m['rate_per_day']}/d"
        max_s = f"{m['rewards_max_spread']}c"
        yes_b = m['books'].get('Yes')
        if yes_b:
            spr = f"{yes_b['spread_cents']:.1f}c"
            q_usd = f"${yes_b['total_qualifying_usd']:,.0f}"
            t_usd = f"${yes_b['touch_bid_usd'] + yes_b['touch_ask_usd']:,.0f}"
            log.info(f"{q:<38} | {d:<10} | {spr:<8} | {max_s:<8} | {q_usd:<14} | {t_usd}")

if __name__ == "__main__":
    main()
