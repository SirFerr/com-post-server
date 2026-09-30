import threading

from composter import Composter
from controller import Controller
from user import User


def main():
    composter = Composter(capacity=100)

    controller = Controller(composter)

    user = User(
        name="Sergey",
        controller=controller
    )

    controller.set_user(user)

    composter_thread = threading.Thread(
        target=composter.run,
        name="ComposterThread",
        daemon=True
    )

    controller_thread = threading.Thread(
        target=controller.run,
        name="ControllerThread",
        daemon=True
    )

    user_thread = threading.Thread(
        target=user.run,
        name="UserThread",
        daemon=True
    )

    composter_thread.start()
    controller_thread.start()
    user_thread.start()

    print("=== Начальное состояние ===")
    user.send("STATUS")
    composter.send("STATUS")

    user.queue.join()
    composter.queue.join()

    print("\n=== Запуск взаимодействия ===")

    user.send("DEPOSIT", 25)

    user.queue.join()
    controller.queue.join()

    print("\n=== Итоговое состояние ===")

    user.send("STATUS")
    composter.send("STATUS")

    user.queue.join()
    composter.queue.join()

    user.send("STOP")
    controller.send("STOP")
    composter.send("STOP")


if __name__ == "__main__":
    main()