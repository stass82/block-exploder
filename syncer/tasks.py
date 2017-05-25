from interactors import Blockchain, BlockchainSyncer
from gateways import MongoDatabaseGateway
from pymongo import MongoClient
from bitcoinrpc.authproxy import AuthServiceProxy
from celery import Celery
from celery.task import Task
from celery.schedules import crontab
from helpers import only_one, generate_bootstrap
import ConfigParser
import os
import raven
import logging
from raven.contrib.celery import register_signal, register_logger_signal


CONFIG_FILE = os.environ['EXPLODER_CONFIG']
config = ConfigParser.RawConfigParser()
config.read(CONFIG_FILE)


class SyncerCelery(Celery):
    def on_configure(self):
        if config.getboolean('syncer', 'sentry'):
            token1 = config.get('syncer', 'sentry_token1')
            token2 = config.get('syncer', 'sentry_token2')
            path = config.get('syncer', 'sentry_path')
            sentry_url = "https://%s:%s@%s" % (token1, token2, path)
            client = raven.Client(sentry_url)

            # register a custom filter to filter out duplicate logs
            register_logger_signal(client, loglevel=logging.WARNING)

            # hook into the Celery error handler
            register_signal(client)


class SyncTask(Task):
    """
    Checks for new blocks and syncs the db to be up to date with the
    current state of the blockchain
    """
    @only_one(key="SingleTask", timeout=config.getint('syncer', 'task_lock_timeout'))
    def run(self, **kwargs):
        client = MongoClient()
        database = MongoDatabaseGateway(client.exploder, config)
        blockchain = Blockchain(database, config)
        rpc_client = AuthServiceProxy("http://%s:%s@127.0.0.1:8332"
                                      % (config.get('syncer', 'rpc_user'), config.get('syncer', 'rpc_password')))
        syncer = BlockchainSyncer(database, blockchain, rpc_client, config)

        # This is where the real work is done
        syncer.sync_auto()

        # Calculate network statistics
        syncer.calculate_network_stats()


class DailyTask(Task):
    def run(self, **kwargs):
        client = MongoClient()
        database = MongoDatabaseGateway(client.exploder, config)
        blockchain = Blockchain(database, config)

        rpc_client = AuthServiceProxy("http://%s:%s@127.0.0.1:8332"
                                      % (config.get('syncer', 'rpc_user'), config.get('syncer', 'rpc_password')))
        syncer = BlockchainSyncer(database, blockchain, rpc_client, config)
        # syncer.calculate_network_hash_rate()

        bootstrap_dir = config.get('syncer', 'bootstrap_dir')
        generate_bootstrap(
            config.get('syncer', 'datadir_path'),
            os.path.join(bootstrap_dir, 'bootstrap.dat')
        )


app = SyncerCelery('tasks', broker='redis://localhost:6379/0')
app.conf.result_backend = 'redis://localhost:6379/0'
app.tasks.register(SyncTask)
app.tasks.register(DailyTask)

app.conf.beat_schedule = {
    'sync-every-10-seconds': {
        'task': 'syncer.tasks.SyncTask',
        'schedule': 10.0,
    },
    'hashrate-once-a-day': {
        'task': 'syncer.tasks.DailyTask',
        #'schedule': crontab(minute=0, hour=12),  # It's high noon
        'schedule': 10.0
    },
}

app.conf.timezone = 'UTC'