import logging

from .link_parser import OzonLinkParser

logger = logging.getLogger(__name__)


class OzonPlaywrightParser(OzonLinkParser):
    """Compatibility wrapper.

    The active Ozon listing parser is now Selenium + selenium-stealth.
    This class remains so older imports and tests keep working.
    """

    def start_parsing(self):
        logger.info(
            "OzonPlaywrightParser совместим с прежним API, "
            "но внутри использует Selenium stealth"
        )
        return super().start_parsing()
