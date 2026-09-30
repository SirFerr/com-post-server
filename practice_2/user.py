from queue import Queue


class User:
    def __init__(self, name: str, controller):
        assert name.strip(), "Имя пользователя не может быть пустым"
        assert controller is not None

        self.name = name
        self.controller = controller
        self.deposited = 0

        self.queue = Queue()

    def send(self, command: str, amount: int | None = None):
        self.queue.put((command, amount))

    def run(self):
        while True:
            command, amount = self.queue.get()

            if command == "DEPOSIT":
                self.deposit(amount)

            elif command == "STATUS":
                self.print_state()

            elif command == "STOP":
                break

            self.queue.task_done()

    def deposit(self, amount: int):
        assert amount > 0, "Количество должно быть положительным"

        print(
            f"[User] {self.name} запросил загрузку "
            f"{amount} единиц отходов"
        )

        self.controller.send("DEPOSIT", amount)

    def deposit_completed(self, amount: int):
        assert amount > 0

        old_value = self.deposited

        self.deposited += amount

        assert self.deposited == old_value + amount

        print(
            f"[User] Операция завершена. "
            f"Всего загружено: {self.deposited}"
        )

    def print_state(self):
        assert self.deposited >= 0

        print(
            f"[User] Пользователь={self.name}, "
            f"загружено={self.deposited}"
        )