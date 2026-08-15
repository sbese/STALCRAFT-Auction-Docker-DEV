from django.apps import AppConfig


class AuctionConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'auction'

    def ready(self):
        from auction.taskrunner import maybe_autostart_collector
        maybe_autostart_collector()
