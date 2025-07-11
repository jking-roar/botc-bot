import logging

import discord

import bot_client
import bot_impl

_ = bot_impl.__name__  # need to reference the bot module to "install" event handlers


class BotApp:
    @staticmethod
    def run():
        print("Starting bot...")
        print("discord version is " + discord.__version__)
        active = True
        while active:
            try:
                bot_client.client.run(bot_client.get_token())
                print("ending bot...")
                print("ignore errors on bot end. the libraries are not perfect")
                active = False
            except Exception as e:
                logging.exception("Ignoring exception")
                print(str(e))
                print("Restarting the bot")


if __name__ == "__main__":
    BotApp.run()
