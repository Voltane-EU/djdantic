class AccessError(Exception):
    def __init__(self, detail, *args):
        self.detail = detail
