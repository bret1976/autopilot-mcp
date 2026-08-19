from __future__ import annotations

import argparse

from app.config import PRICE_USD, public_base_url
from app.store import ensure_buyer
from app.tokens import clean_buyer_id, mint_token


def mint(email: str, buyer_id: str = "", note: str = "", days: int = 0) -> dict[str, str]:
    slug = clean_buyer_id(buyer_id or email.split("@", 1)[0])
    token = mint_token(slug, days=days)
    record = ensure_buyer(slug, email=email, note=note, token=token)
    url = f"{public_base_url()}/mcp/t/{record['token']}"
    return {"buyer_id": slug, "email": email, "url": url, "price": str(PRICE_USD), "days": str(days)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Mint a signed 6Frame Autopilot MCP URL.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--buyer-id", default="")
    parser.add_argument("--note", default="")
    parser.add_argument("--days", type=int, default=0, help="0 = no expiry")
    args = parser.parse_args()
    result = mint(args.email, args.buyer_id, args.note, args.days)
    print(f"{result['email']}  ${result['price']} once")
    print(result["url"])


if __name__ == "__main__":
    main()
