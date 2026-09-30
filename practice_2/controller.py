from queue import Queue


class Controller:
    def __init__(self, composter):
        assert composter is not None

        self.composter = composter
        self.user = None

        self.queue = Queue()

    def set_user(self, user):
        assert user is not None
        self.user = user

    def send(self, command: str, amount: int | None = None):
        self.queue.put((command, amount))

    def run(self):
        while True:
            command, amount = self.queue.get()

            if command == "DEPOSIT":
                self.process_deposit(amount)

            elif command == "STOP":
                break

            self.queue.task_done()

    def process_deposit(self, amount: int):
        assert self.user is not None, "Пользователь не назначен"
        assert amount > 0, "Количество должно быть положительным"

        print("[Controller] Начало операции")

        self.composter.send("OPEN")
        self.composter.queue.join()

        self.composter.send("ADD", amount)
        self.composter.queue.join()

        self.composter.send("CLOSE")
        self.composter.queue.join()

        self.user.deposit_completed(amount)

        print("[Controller] Операция завершена")