# vLLM на CPU в Kubernetes

Инференс `Qwen2.5-1.5B-Instruct` через vLLM (**≥ 0.27.0**, строго CPU) в Kubernetes / Minikube:
rootless-образ, зафиксированные зависимости, логи без промптов и ответов, кэш весов в PVC,
секреты в `Secret`, параметры запуска в `ConfigMap`.

## Структура

| Путь | Что это |
|---|---|
| `Dockerfile` | Образ vLLM (CPU): непривилегированный пользователь `10001`, установка по `requirements.txt` с хэшами |
| `requirements.in` / `requirements.txt` | Верхнеуровневая зависимость / полный пин (с транзитивными, с хэшами), генерируется `scripts/lock.sh` |
| `privacy_logging/` | Фильтр логов (`vllm_privacy_logging.py`) и `logging_config.json` для `VLLM_LOGGING_CONFIG_PATH` |
| `docker/entrypoint.sh` | Собирает аргументы `vllm serve` из переменных ConfigMap/Secret |
| `downloader/` | Скрипт загрузки весов (`download_model.py`) и отдельный лёгкий rootless-`Dockerfile` для initContainer |
| `k8s/` | Манифесты + `kustomization.yaml` (`kubectl apply -k k8s/`); параметры vLLM — `k8s/vllm-config.env` |
| `loadtest/loadtest.py` | Нагрузочный тест (TTFT, Latency, Throughput), только stdlib |
| `tests/` | Тест фильтра логов и мок-сервер для проверки нагрузочного скрипта |

## Как это работает

- **Rootless.** Образы запускаются от uid/gid `10001`; в поде `runAsNonRoot`, `capabilities: drop ALL`,
  `allowPrivilegeEscalation: false`, `seccompProfile: RuntimeDefault`.
- **Воспроизводимость.** `scripts/lock.sh` (`uv pip compile --generate-hashes`) пишет все прямые и транзитивные
  зависимости с хэшами; `Dockerfile` ставит их через `pip install --require-hashes --no-deps`.
- **Приватность логов.** `VLLM_LOGGING_CONFIG_PATH` подключает `logging.Filter` на handler логгера `vllm`.
  Записи RequestLogger (там vLLM пишет промпт и ответ: `vllm.entrypoints.serve.utils.request_logger` в 0.27,
  `vllm.entrypoints.logger` в более старых релизах) заменяются на
  `model request received: id=<request-id> (prompt/output not logged)`; в остальных записях значения после
  `prompt/output/...` заменяются на `[REDACTED]`. Дополнительно запуск идёт с `--max-log-len 0`.
  Факт обращения остаётся в логе (request id + строки access-лога uvicorn с путём и кодом ответа).
- **Кэш весов.** `initContainer` (`downloader/download_model.py`) скачивает модель в PVC `vllm-models` и в конце
  пишет маркер `.download_complete`. При рестартах маркер найден — сеть не используется. Основной контейнер
  работает с `HF_HUB_OFFLINE=1`, токен HF ему не передаётся.
- **Секреты.** `Secret vllm-secrets` (создаётся kustomize из `k8s/secrets.env`): `HF_TOKEN` (только initContainer)
  и `VLLM_API_KEY` (Bearer-токен клиентов, vLLM читает его из окружения, в аргументах процесса его нет).
- **Конфигурация.** Всё, что влияет на инференс, лежит в `k8s/vllm-config.env`, из которого kustomize
  (`configMapGenerator`) создаёт ConfigMap `vllm-config`. Меняете значение, делаете `kubectl apply -k k8s/`:
  хэш в имени ConfigMap меняется, и Deployment перекатывает под с новыми настройками без пересборки образа.

## Запуск (Minikube)

Требуется Docker, minikube, kubectl, uv; минимум 12 ГиБ RAM и 4 vCPU для кластера
(под запрашивает 2 CPU / 6 ГиБ, лимит памяти 10 ГиБ: веса в float32 занимают ~6 ГиБ).
Больше vCPU (`--cpus`) ускоряет инференс; результаты ниже сняты на ноде с 18 vCPU.

```bash
# 0. Зафиксировать зависимости (один раз, нужен интернет), результат закоммитить
./scripts/lock.sh

# 1. Кластер
minikube start --cpus 4 --memory 12g

# 2. Собрать образы локальным Docker и загрузить их в minikube
#    (работает при любом рантайме minikube, в том числе containerd, где docker-env не подходит)
docker build -t vllm-cpu:0.27.1 .
docker build -f downloader/Dockerfile -t vllm-model-downloader:1.0 .
minikube image load vllm-cpu:0.27.1
minikube image load vllm-model-downloader:1.0

# 3. Секреты (файл в .gitignore)
cp k8s/secrets.env.example k8s/secrets.env
sed -i.bak "s/^VLLM_API_KEY=.*/VLLM_API_KEY=$(openssl rand -hex 32)/" k8s/secrets.env && rm k8s/secrets.env.bak
#   HF_TOKEN оставьте пустым для публичной модели; для gated/private впишите свой токен

# 4. Деплой
kubectl apply -k k8s/
kubectl -n vllm rollout status deploy/vllm --timeout=30m
kubectl -n vllm logs deploy/vllm -c model-downloader   # первый запуск: скачивание; рестарт: "skipping network access"

# 5. Проверка
kubectl -n vllm port-forward svc/vllm 8000:8000 &
export API_KEY=$(grep ^VLLM_API_KEY k8s/secrets.env | cut -d= -f2)
curl -s localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5-1.5b-instruct","messages":[{"role":"user","content":"Привет!"}],"max_tokens":32}'
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/v1/models        # без токена -> 401

# 6. Проверка приватности логов: в выводе должен быть request id, но не текст «Привет!»
kubectl -n vllm logs deploy/vllm | grep -i "model request"
kubectl -n vllm logs deploy/vllm | grep -c "Привет" || true            # ожидаем 0

# 7. Проверка кэша: рестарт пода не должен снова качать веса
kubectl -n vllm rollout restart deploy/vllm && kubectl -n vllm rollout status deploy/vllm
kubectl -n vllm logs deploy/vllm -c model-downloader                   # "skipping network access"
```

Локальные проверки без кластера: `python3 tests/test_privacy_logging.py`.

## Нагрузочное тестирование

```bash
export API_KEY=...   # тот же Bearer-токен
python3 loadtest/loadtest.py --url http://localhost:8000 --model qwen2.5-1.5b-instruct \
    --requests 8 --concurrency 1 4 8 --warmup 1 --timeout 1800 --out loadtest/results/results.md
```

Сценарии: `short` (~16 слов, `max_tokens=64`) и `long` (~1500 слов ≈ 2k токенов, `max_tokens=64`).
Метрики: **TTFT** — до первого чанка с контентом (SSE); **Latency** — полное время запроса;
**Throughput** — выходные токены в секунду по всем запросам (`usage.completion_tokens`) и req/s.
Скрипт сам прогревает модель (`--warmup`) и показывает p50/p95. В начало каждого промпта ставится уникальная
метка: vLLM по умолчанию кэширует префиксы, и с одинаковыми промптами префилл длинного запроса считался бы
только один раз (TTFT получался бы ~1 с вместо ~17 с).

### Результаты

Стенд: Intel Core Ultra 5 125H (AVX2/AVX-VNNI, без AVX512), Windows 11 + WSL2, Minikube (driver docker,
runtime containerd, нода 18 vCPU / 12 ГиБ). vLLM 0.27.1+cpu, `float32`, `--enforce-eager`, `MAX_NUM_SEQS=16`,
`MAX_NUM_BATCHED_TOKENS=2048`, KV-кэш 1 ГиБ; vLLM сам выбрал 8 OpenMP-потоков. По 8 запросов на сценарий.

| Prompt | Concurrency | TTFT p50, s | TTFT p95, s | Latency p50, s | Latency p95, s | Throughput, tok/s | req/s |
|---|---|---|---|---|---|---|---|
| short | 1 | 1.20 | 1.24 | 5.64 | 8.80 | 4.3 | 0.16 |
| short | 4 | 4.13 | 4.14 | 12.15 | 15.43 | 7.6 | 0.29 |
| short | 8 | 7.23 | 7.23 | 19.27 | 22.37 | 11.4 | 0.36 |
| long | 1 | 16.79 | 17.26 | 21.79 | 22.34 | 1.2 | 0.05 |
| long | 4 | 33.53 | 68.50 | 75.93 | 110.49 | 1.3 | 0.05 |
| long | 8 | 90.99 | 135.74 | 145.73 | 146.14 | 1.4 | 0.05 |

Наблюдения:
- **Короткие промпты** упираются в декод: батчинг почти линейно поднимает throughput (4.3 → 11.4 tok/s при
  concurrency 1 → 8), ценой роста латентности каждого запроса.
- **Длинные промпты** упираются в префилл (~120 входных токенов/с): параллельные запросы почти не добавляют
  throughput, они только стоят в очереди, и TTFT растёт пропорционально их числу.
- `bfloat16` на этом CPU эмулируется (нет AVX512_BF16/AMX) и был в ~7 раз медленнее `float32`, поэтому в
  ConfigMap выбран `float32`.

### Что менять для Throughput

Особенность CPU-бэкенда: декодирование упирается в пропускную способность памяти, префилл — в вычисления.

**А) Много запросов с короткими промптами/ответами**
- Поднять `MAX_NUM_SEQS` (больше запросов в одном батче): на декоде веса читаются один раз на весь батч, это
  главный рычаг throughput. Растёт и потребление KV-кэша, поэтому согласованно увеличить `VLLM_CPU_KVCACHE_SPACE`
  (и memory limit пода).
- Уменьшить `MAX_MODEL_LEN` до реально нужного, чтобы KV-кэш вмещал больше одновременных последовательностей.
- `MAX_NUM_BATCHED_TOKENS` можно оставить умеренным (≥ `MAX_NUM_SEQS`): префиллы короткие, батч набирается из декода.
- `--enable-prefix-caching` (через `EXTRA_ARGS`), если у запросов общий системный промпт.
- Привязать OpenMP-потоки к физическим ядрам (`VLLM_CPU_OMP_THREADS_BIND`), отключить HT/SMT, использовать
  tcmalloc (уже в образе) и bf16/INT8 вместо fp32 (bf16 выгоден только при AVX512_BF16/AMX, иначе он эмулируется); при нехватке ядер на одну реплику — несколько реплик вместо
  одной огромной.

**Б) Мало запросов, но огромный контекст**
- Нагрузка — это префилл (compute-bound). Включить/оставить chunked prefill и поднять `MAX_NUM_BATCHED_TOKENS`
  (например, 4096–8192): префилл идёт крупными кусками, лучше загружая ядра; но слишком большое значение ухудшает
  задержки декода у соседних запросов.
- `MAX_MODEL_LEN` — под длину контекста; `VLLM_CPU_KVCACHE_SPACE` — под `MAX_NUM_SEQS × длина контекста`
  (KV-кэш одной длинной последовательности большой).
- `MAX_NUM_SEQS` можно уменьшить: одновременно держать много длинных запросов нет смысла, они упрутся в
  память/KV-кэш.
- Prefix caching, если контекст повторяется между запросами (общий документ).
- Квантизация (INT8 W8A8/AWQ/GPTQ, если поддерживается вашей сборкой) уменьшает память и ускоряет вычисления;
  при нескольких NUMA-узлах — tensor parallel по узлам.

> Названия и поведение флагов vLLM меняются от релиза к релизу: перед использованием сверьтесь с
> `vllm serve --help` и документацией вашей версии.

## Ограничения и заметки

- Образ рассчитан на x86_64 (колесо `manylinux_2_34_x86_64`). Для ARM замените колесо в `requirements.in` и
  `--python-platform` в `scripts/lock.sh`.
- `readOnlyRootFilesystem` не включён: vLLM/torch пишут кэш компиляции; записываемые пути вынесены в `emptyDir`.
- Реестр: образы собираются локально и загружаются в minikube через `minikube image load` (`imagePullPolicy: IfNotPresent`); для реального кластера
  запушьте их в реестр и поправьте блок `images:` в `k8s/kustomization.yaml`.
