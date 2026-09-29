# Сервис backup: pg_dump той же версии, что сервер, и rclone для копии
# вне сервера. rclone берётся готовым бинарником из официального образа
# проекта: статическая сборка Go, работает в alpine как есть, и в
# контейнере не нужен ни apk, ни интернет при старте.
FROM rclone/rclone:1.75 AS rclone

FROM postgres:16-alpine
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
# Корневые сертификаты - от того же образа, с которым rclone собран и
# проверен: без них HTTPS до хранилища не поднимется.
COPY --from=rclone /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY backup.sh /usr/local/bin/backup.sh
# Концы строк CRLF (архив, собранный на Windows) sh читает как часть
# команды: «set: Illegal option -», и сервис не делает даже дампа на
# диске. Чинится здесь, чем бы файл ни приехал.
RUN sed -i 's/\r$//' /usr/local/bin/backup.sh && chmod 0755 /usr/local/bin/backup.sh
ENTRYPOINT ["/bin/sh", "/usr/local/bin/backup.sh"]
CMD ["loop"]
