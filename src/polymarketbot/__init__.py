from polymarketbot.config import BotConfig, load_config
from polymarketbot.logs import get_logger

log = get_logger("polymarketbot")

__all__ = ["BotConfig", "load_config"]


def main() -> None:
    log.info("Initialized polymarketbot engine.")
