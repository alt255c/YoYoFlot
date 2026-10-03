# YoYoFlot: Catch The Spies

## О датасете

Для обучения модели использовался датасет, доступный в публичной папке Google Drive:

🔗 **Ссылка на датасет:**  
[https://drive.google.com/drive/folders/19bCT5pKF-QnfW05FW0Eb2dUsMrrnbUSD?usp=sharing](https://drive.google.com/drive/folders/19bCT5pKF-QnfW05FW0Eb2dUsMrrnbUSD?usp=sharing)

Вы можете скачать все файлы вручную по ссылке выше или воспользоваться скриптами, которые автоматически загрузят их в папку `./data`.

## Быстрый старт

### Windows (PowerShell)

1. Откройте PowerShell от имени администратора (или обычного пользователя).
2. Перейдите в корень проекта:
    ```powershell
    cd путь_к_проекту
    ```
3. Разрешите выполнение сценариев:
    ```powershell
    Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
    ```
4. Запустите скрипт:
    ```powershell
    .\download_data.ps1
    ```

### Linux / macOS (Bash)
1. Откройте терминал.
2. Перейдите в корень проекта:
    ```bash
    cd путь_к_проекту
    ```
3. Сделайте скрипт исполняемым и запустите:
    ```bash
    chmod +x download_data.sh
    ./download_data.sh
    ```
После завершения работы скрипта все файлы датасета будут находиться в папке ./data.

## Требования
* Python 3.10+ (для установки gdown)
* pip (менеджер пакетов Python)
* PowerShell 5.1+ (Windows) или Bash 4.x+ (Linux/macOS)

> !!! Скрипты автоматически проверяют наличие gdown и при необходимости устанавливают его через pip.