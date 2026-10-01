class _Loop:
    def __init__(self, func):
        self.func, self.seconds = func, 300

    def before_loop(self, func):
        return func

    def change_interval(self, seconds):
        self.seconds = seconds


class tasks:
    @staticmethod
    def loop(**_):
        return _Loop
