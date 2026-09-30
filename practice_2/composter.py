from queue import Queue


class Composter:
    def __init__(self, capacity: int):
        assert capacity > 0, "Вместимость должна быть положительной"

        self.capacity = capacity
        self.current_load = 0
        self.is_open = False

        self.queue = Queue()

    def send(self, command: str, amount: int | None = None):
        self.queue.put((command, amount))

    def run(self):
        while True:
            command, amount = self.queue.get()

            if command == "OPEN":
                self.open()

            elif command == "ADD":
                self.add_waste(amount)

            elif command == "CLOSE":
                self.close()

            elif command == "STATUS":
                self.print_state()

            elif command == "STOP":
                break

            self.queue.task_done()

    def open(self):
        assert not self.is_open, "Компостер уже открыт"

        self.is_open = True

        assert self.is_open, "Компостер должен быть открыт"

        print("[Composter] Компостер открыт")

    def add_waste(self, amount: int):
        assert self.is_open, "Перед загрузкой компостер должен быть открыт"
        assert amount is not None
        assert amount > 0, "Количество отходов должно быть положительным"
        assert self.current_load + amount <= self.capacity, \
            "Превышена вместимость компостера"

        old_load = self.current_load

        self.current_load += amount

        assert self.current_load == old_load + amount
        assert 0 <= self.current_load <= self.capacity

        print(f"[Composter] Добавлено отходов: {amount}")

    def close(self):
        assert self.is_open, "Компостер уже закрыт"

        self.is_open = False

        assert not self.is_open

        print("[Composter] Компостер закрыт")

    def print_state(self):
        assert 0 <= self.current_load <= self.capacity

        print(
            f"[Composter] Состояние: "
            f"{self.current_load}/{self.capacity}, "
            f"открыт={self.is_open}"
        )