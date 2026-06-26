from .publicsearch import PublicSearchScraper


class BexarCountyScraper(PublicSearchScraper):
    def __init__(self):
        super().__init__('bexar', 'Bexar')


class DallasCountyScraper(PublicSearchScraper):
    def __init__(self):
        super().__init__('dallas', 'Dallas')


class TarrantCountyScraper(PublicSearchScraper):
    def __init__(self):
        super().__init__('tarrant', 'Tarrant')


class DentonCountyScraper(PublicSearchScraper):
    def __init__(self):
        super().__init__('denton', 'Denton')


class JohnsonCountyScraper(PublicSearchScraper):
    def __init__(self):
        super().__init__('johnson', 'Johnson')
