from .publicsearch import PublicSearchScraper


class BexarCountyScraper(PublicSearchScraper):
    def __init__(self):
        super().__init__(
            county_slug='bexar',
            county_name='Bexar',
            doc_types=['NTS', 'NOTSALE', 'FRCL'],
        )


class DallasCountyScraper(PublicSearchScraper):
    def __init__(self):
        super().__init__(
            county_slug='dallas',
            county_name='Dallas',
            doc_types=['NTS', 'NOTSALE', 'FRCL'],
        )


class TarrantCountyScraper(PublicSearchScraper):
    def __init__(self):
        super().__init__(
            county_slug='tarrant',
            county_name='Tarrant',
            doc_types=['NTS', 'NOTSALE', 'FRCL'],
        )
