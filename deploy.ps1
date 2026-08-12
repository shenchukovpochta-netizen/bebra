# Заливка проекта на VPS с Windows. Требует OpenSSH (встроен в Windows 10/11).
#
#   .\deploy.ps1 -Server root@123.45.67.89
#
# .env и secrets/ не передаются: заполняются и генерируются на сервере.

param(
  [Parameter(Mandatory = $true)][string]$Server,
  [string]$Path = '/opt/mybike',
  [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

if (-not $SkipTests) {
  Step 'тесты'
  py -3 -m unittest discover -s tests
  if ($LASTEXITCODE -ne 0) { throw 'тесты не прошли, деплой остановлен' }

  Step 'проверка синтаксиса всех модулей'
  py -3 -m compileall -q app
  if ($LASTEXITCODE -ne 0) { throw 'модули не компилируются' }

  # Сверка файлов между собой: переменная объявлена в одном месте и забыта
  # в другом, файл есть в проекте но не попал в список заливки. Тесты этого
  # не видят - на таких расхождениях проект уже спотыкался дважды.
  Step 'сверка файлов между собой'
  py -3 consistency.py
  if ($LASTEXITCODE -ne 0) { throw 'найдены расхождения между файлами, деплой остановлен' }
}

# Явные списки вместо scp -r: так .env, secrets/ и __pycache__
# не уедут на сервер по случайности.
$root = @('docker-compose.yml', 'Dockerfile', 'pyproject.toml', 'requirements.txt',
          'schema.sql', 'bootstrap.sh', 'install.sh', '.env.example', '.gitignore',
          'README.md', 'INSTALL.md', 'consistency.py')
$app = @('app/__init__.py', 'app/main.py', 'app/max_main.py', 'app/config.py',
         'app/db.py',
         'app/logic.py', 'app/faq.py', 'app/faq_i18n.py', 'app/texts.py',
         'app/i18n/__init__.py', 'app/i18n/en.py', 'app/i18n/uz.py',
         'app/i18n/tk.py', 'app/i18n/ar.py', 'app/i18n/fa.py',
         'app/i18n/hi.py', 'app/i18n/cv.py',
         'app/keyboards.py',
         'app/middlewares.py',
         'app/filters.py', 'app/tasks.py', 'app/contract_template.docx',
         'app/act_priema_template.docx', 'app/act_vozvrata_template.docx',
         'app/soglasie_template.docx', 'app/pdn_policy.docx')
$handlers = @('app/handlers/__init__.py', 'app/handlers/registration.py',
              'app/handlers/moderation.py', 'app/handlers/menu.py',
              'app/handlers/contract.py', 'app/handlers/faq.py')
$services = @('app/services/__init__.py', 'app/services/subscription.py',
              'app/services/files.py',
              'app/services/contract.py', 'app/services/crypto.py')
$max = @('app/max/__init__.py', 'app/max/client.py', 'app/max/parse.py',
         'app/max/keyboards.py', 'app/max/handlers.py', 'app/max/runner.py')
$tests = @('tests/__init__.py', 'tests/test_logic.py', 'tests/test_config.py',
          'tests/test_sql.py', 'tests/test_flow.py', 'tests/test_contract.py',
          'tests/test_max.py', 'tests/test_faq.py', 'tests/test_i18n.py')

foreach ($f in ($root + $app + $handlers + $services + $max + $tests)) {
  if (-not (Test-Path $f)) { throw "нет файла $f" }
}

Step "создаю каталоги на $Server"
ssh $Server "mkdir -p '$Path/app/handlers' '$Path/app/services' '$Path/app/max' '$Path/tests'"
if ($LASTEXITCODE -ne 0) { throw 'не удалось подключиться по SSH' }

Step 'копирую файлы'
scp $root      "${Server}:${Path}/"
if ($LASTEXITCODE -ne 0) { throw 'scp (корень) не удался' }
scp $app       "${Server}:${Path}/app/"
if ($LASTEXITCODE -ne 0) { throw 'scp (app) не удался' }
scp $handlers  "${Server}:${Path}/app/handlers/"
if ($LASTEXITCODE -ne 0) { throw 'scp (handlers) не удался' }
scp $services  "${Server}:${Path}/app/services/"
if ($LASTEXITCODE -ne 0) { throw 'scp (services) не удался' }
scp $max       "${Server}:${Path}/app/max/"
if ($LASTEXITCODE -ne 0) { throw 'scp (max) не удался' }
scp $tests     "${Server}:${Path}/tests/"
if ($LASTEXITCODE -ne 0) { throw 'scp (tests) не удался' }

# CRLF в .sh ломает shebang: bash ругается на «\r: команда не найдена»
Step 'нормализую переводы строк'
ssh $Server "cd '$Path' && sed -i 's/\r`$//' bootstrap.sh install.sh && chmod +x bootstrap.sh install.sh"

Write-Host "`nФайлы на сервере. Дальше:" -ForegroundColor Green
Write-Host "  ssh $Server"
Write-Host "  cd $Path"
Write-Host "  bash install.sh"
Write-Host ""
Write-Host "install.sh задаст вопросы, проверит каждый ответ через Telegram," -ForegroundColor DarkGray
Write-Host "сам сгенерирует секреты и запустит бота. Править .env не нужно." -ForegroundColor DarkGray
