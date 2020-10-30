[![manaakiwhenua-standards](https://github.com/manaakiwhenua/pygeoapi-redis-manager-plugin/workflows/manaakiwhenua-standards/badge.svg)](https://github.com/manaakiwhenua/manaakiwhenua-standards)

**In-development; do not use for production applications.**

Contributions welcome.

# Pygeoapi Redis manager plugin

A [pygeoapi](https://github.com/geopython/pygeoapi) processing manager plugin. This plugin is a drop-in replacement plugin for the default pygeoapi processing manager. The default manager (as of pygeoapi v0.8)
uses the file system as an output cache, and is only capable of doing jobs asynchronously by spawning new threads. This plugin introduces the use of Redis as a separate microservice to store job state, and also the use of RQ (Redis Queue) to manager worker processes that do the actual computation.

It is capable of supporting any processing task with input that can be de/serialised to/from JSON. This means that file uploads and other complex inputs are not supported.

## Sample usage

Here is a sample Docker Compose YAML demonstrating the use of pygeoapi, this plugin, with separate Redis (broker) and RQ (worker) microservices.

```yaml
version: "3"
services:

  pygeoapi:
    build: .
    image: pygeoapi
    container_name: pygeoapi
    ports:
      - "5000:80"
    environment:
      - TZ=Pacific/Auckland
      - LANG=en_NZ.UTF-8
    volumes:
      - ./local.config.yml:/pygeoapi/local.config.yml
      - ./pygeoapi:/pygeoapi/pygeoapi
      - ./tests:/pygeoapi/tests
      - /etc/localtime:/etc/localtime:ro
    depends_on:
      - broker
      - worker
    networks:
      frontend:
        ipv4_address: 172.21.0.24

  broker:
    image: redis:6.0.3
    container_name: broker
    command:
      - redis-server
      - /etc/redis.conf
    environment:
      - REDISCLI_AUTH=some-long-random-password
    networks:
      frontend:
        ipv4_address: 172.21.0.3
    ports:
      - '6379'
    volumes:
      - "./redis-data:/data"
      - "./redis.conf:/etc/redis.conf"

  worker:
    image: pygeoapi
    entrypoint: python3
    restart: always
    command: /usr/local/lib/python3.8/dist-packages/pygeoapi_redis_manager_plugin/redis_worker_.py -q hello-world -q wkt-reprojector
    volumes:
      - ./pygeoapi:/pygeoapi/pygeoapi
    networks:
      - frontend
    environment:
      - REDIS=172.21.0.3
      - PORT=6379
    depends_on:
      - broker

volumes:
  redis-data:

networks:
  frontend:
    ipam:
      config:
        - subnet: 172.21.0.0/24
```

This depends on your pygeoapi instance being built with an additional dependency:

`requirements-processes.txt`
```txt
git+https://github.com/manaakiwhenua/pygeoapi-redis-manager-plugin.git@master
```

Additionally, add this to `pygeoapi/plugin.py` (pending a better way to integrate plugins into pygeoapi):

```python
'process_manager': {
    'TinyDB': 'pygeoapi.process.manager.tinydb_.TinyDBManager',
    'Redis': 'pygeoapi_redis_manager_plugin.RedisManager' # <---
}
```

Finally, set your pygeoapi YAML configuration to include this under the `server` block:

```yaml
manager:
  name: Redis
  connection: 172.21.0.3
```

If you run `docker-compose up --scale worker=4`, then all the defined services will be brought up in orchestra, with four worker processes running. This means that as many as four processing jobs can be executed at once, with any other jobs after that being put onto the worker queue and executed as workers complete existing jobs.

The `worker` `command` `/pygeoapi/pygeoapi/process/manager/redis_worker_.py -q hello-world -q wkt-reprojector` includes two job queues, `hello-world` and `wkt-reprojector`. The first is the built-in sample process for pygeoapi; the second is defined as a [plugin](https://github.com/manaakiwhenua/wkt-reprojector-plugin) (incude in your `requirements-processes.txt` as `git+https://github.com/manaakiwhenua/wkt-reprojector-plugin.git@master` or otherwise follow instructions in that repository). Workers need to be told which queues to work on, and do not need to work on all of them.

Workers depend on two environment variables, `REDIS` and `PORT` which specify the hostname or IP address of the redis instance they can use, and the appropriate port.

### Redis configuration

In addition, you will need to set up the `redis.conf` (Redis configuration file) that matches these binds (if appropriate for your networking pattern). You can get a default Redis configuration for your version of Redis [here](https://redis.io/topics/config)

> By default, if no "bind" configuration directive is specified, Redis listens for connections from all the network interfaces available on the server. It is possible to listen to just one or multiple selected interfaces using the "bind" configuration directive, followed by one or more IP addresses.

Add this line (or replace the line beginning with `bind`) to your `redis.conf`:

```
bind 172.21.0.3
```

This matches the block in the `docker-compose.yml`:

```yaml
services:
  broker:
    networks:
      frontend:
        ipv4_address: 172.21.0.3 # <----
```

### Dashboard

If you define an additional service:

```yaml
dashboard:
  build: ./dashboard
  image: dashboard
  container_name: dashboard
  ports:
    - 9181:9181
  command: rq-dashboard -H 172.21.0.3
  networks:
    frontend:
      ipv4_address: 172.21.0.16
  depends_on:
    - broker
    - worker
```

Where there is a file `./dashboard/Dockerfile`:

```Dockerfile
FROM python:3.8.0-alpine

RUN pip install rq-dashboard

EXPOSE 9181

CMD ["rq-dashboard"]
```

You can use the RQ Dashboard to inspect jobs and workers. This is not a production application, but it is useful and fun in development.

Pygeoapi's process state is independent of the RQ state, although both are managed in the same Redis database (the `broker` service above).

### Volumes

Using the redis-data volume means that the database created and used by the `broker` service will be available on the host OS, and can therefore be persisted, backed-up, audited and removed.

## Building for release

Requires wheel.

`python setup.py sdist bdist_wheel`

This can be included in a requirements.txt as: `git+https://github.com/manaakiwhenua/pygeoapi-redis-manager-plugin.git@master`

`master` branch is for release, changes should be proposed in a separate branch and a PR submitted for merging into master, including rebuilding the source distributions.
