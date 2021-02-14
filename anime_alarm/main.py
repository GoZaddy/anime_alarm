import os
from pprint import pprint
from app_config import config, client, updater, users, animes, all_users_by_anime, scraper, log_error, \
    sort_anime_by_followers, anime_by_id, logger, log_file_path
from scraping import CannotDownloadAnimeException
from faunadb import query as q, errors
from telegram.ext import CommandHandler, MessageHandler, CallbackQueryHandler, Filters, CallbackContext, Job, \
    JobQueue
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv
from telegram.error import TelegramError, Unauthorized, BadRequest, TimedOut, ChatMigrated, NetworkError
from multiprocessing import Pool
from my_workaround import send_broadcast
from shorten_link import shorten
import datetime
from models import User
from decorators import admin_only, mark_inactive
import sentry_sdk

# set up sentry
sentry_sdk.init(
    "https://bc6863c9bd174d5a8cd5f95f6d45f4b0@o462758.ingest.sentry.io/5595618",
    traces_sample_rate=1.0
)

# load environment variables
load_dotenv()

# setting up telegram stuff
dispatcher = updater.dispatcher
job_queue = updater.job_queue

maintenance_message = 'Bot is currently undergoing maintenance and upgrades'


def get_subscribed_users_for_anime(anime_doc_id):
    subscribed_users = []
    subscribed_users = client.query(
        q.map_(
            q.lambda_('doc_ref', q.get(q.var('doc_ref'))),
            q.paginate(q.match(q.index(all_users_by_anime), q.ref(q.collection(animes), str(anime_doc_id))),
                       size=100000)
        )
    )
    subscribed_users = subscribed_users['data']
    return subscribed_users


def send_update_to_subscribed_users(anime: dict, download_link=None, anime_info=None):
    if isinstance(anime, dict):
        pass
    elif isinstance(anime, str) or isinstance(anime, int):
        anime = client.query(
            q.get(q.ref(q.collection(animes), str(anime)))
        )

    if anime_info is None:
        anime_info = scraper.get_anime_info(anime['data']['link'])

    if anime_info['number_of_episodes'] > anime['data']['episodes']:
        if anime_info['latest_episode_link'] != anime['data']['last_episode']['link']:
            try:
                if download_link is None:
                    download_link = shorten(scraper.get_download_link(anime_info['latest_episode_link']))
                else:
                    pass
            except CannotDownloadAnimeException:
                subscribed_users = get_subscribed_users_for_anime(anime['ref'].id())
                # tell subscribed user episode is available but can't download
                for user in subscribed_users:
                    text = "A new episode for {0}: {1} is now out.\nSadly, I could not download it\U0001F622".format(
                        anime['data']['title'], anime_info['latest_episode_title'])
                    updater.bot.send_message(chat_id=int(user['ref'].id()), text=text)
                # send message to admin
                updater.bot.send_message(chat_id=os.getenv('ADMIN_CHAT_ID'), text=anime['data'][
                                                                                      'title'] + 'just got a new '
                                                                                                 'episode but could '
                                                                                                 'not be downloaded')
            else:
                subscribed_users = get_subscribed_users_for_anime(anime['ref'].id())

                markup = [[InlineKeyboardButton(text='Download', url=download_link)]]
                # send message to subscribed users

                for user in subscribed_users:
                    try:
                        text = "Here's the latest episode for {0}:\n\n{1}".format(anime['data']['title'],
                                                                                  anime_info['latest_episode_title'])
                        updater.bot.send_message(chat_id=int(user['ref'].id()), text=text,
                                                 reply_markup=InlineKeyboardMarkup(markup))
                    except Unauthorized as err:
                        # user has blocked bot
                        # delete user from list
                        client.query(
                            q.delete(
                                user['ref'],
                            )
                        )
                        logger.info("A user has been deleted from user list")
                # send message to admin
                updater.bot.send_message(chat_id=os.getenv('ADMIN_CHAT_ID'),
                                         text=anime['data']['title'] + ' just got a new episode and was updated!')
                logger.info(
                    str(len(subscribed_users)) + " users were notified of an update to " + anime['data']['title'])

            finally:
                # update anime in db after sending messages to users
                client.query(
                    q.update(
                        anime['ref'],
                        {
                            'data': {
                                'episodes': anime_info['number_of_episodes'],
                                'last_episode': {
                                    'title': anime_info['latest_episode_title'],
                                    'link': anime_info['latest_episode_link']
                                }
                            }
                        }
                    )
                )

        else:
            pass
    else:
        pass


def run_cron():
    print('running')

    def check_for_update(context: CallbackContext):
        print('about to run subscription check')
        # updater.bot.send_message(chat_id=os.getenv('ADMIN_CHAT_ID'), text='About to run subscription check!')
        logger.info("About to run subscription check")
        # get all anime
        all_animes = client.query(
            q.paginate(q.documents(q.collection(animes)), size=100000)
        )

        for anime in all_animes['data']:
            # get anime_info in the function send_update...
            # if there are new episodes...
            send_update_to_subscribed_users(anime.id())

        # context.bot.send_message(chat_id=os.getenv('ADMIN_CHAT_ID'), text='Subscription check finished!')
        logger.info("Subscription check finished")

    try:
        # run job every 4 hours
        # this automatically runs in a separate thread so no wahala
        job_queue.run_repeating(check_for_update, interval=7200,
                                first=datetime.datetime.now() + datetime.timedelta(seconds=5))
    except Exception as err:
        log_error(err)


@mark_inactive(message=maintenance_message)
def plain_message(update: Update, context: CallbackContext):
    print(update.effective_message)
    try:
        bot_user = client.query(q.get(q.ref(q.collection('users'), update.effective_chat.id)))
    except errors.NotFound:
        context.bot.send_message(chat_id=update.effective_chat.id,
                                 text='Sorry, I do not understand what you mean.\nPlease use the /help command to '
                                      'discover what I can help you with.')
        return
    user = User(update.effective_chat.id)
    last_command = bot_user['data']['last_command']
    message = update.message.text

    print(last_command)

    if last_command == 'subscribe':
        try:
            search_results = scraper.get_anime(message, limit=15)
            if len(search_results) == 0:
                context.bot.send_message(chat_id=user.chat_id,
                                         text='Sorry but no search results were available for this anime')
            else:
                context.bot.send_message(chat_id=user.chat_id, text='Here are the search results for ' + message)
                for result in search_results:
                    markup = [[InlineKeyboardButton('Select', callback_data='subscribe=' + shorten(result['link']))]]
                    context.bot.send_photo(chat_id=user.chat_id, photo=result['thumbnail'], caption=result['title'],
                                           timeout=5, reply_markup=InlineKeyboardMarkup(markup))

            # update last command
            user.update_last_command('')

        except Exception as err:
            log_error(err)

    elif last_command == 'getlatest':
        try:
            search_results = scraper.get_anime(message, limit=15)

            if len(search_results) == 0:
                context.bot.send_message(chat_id=user.chat_id,
                                         text='Sorry but no search results were available for this anime')
            else:
                context.bot.send_message(chat_id=user.chat_id, text='Here are the search results for ' + message)
                for result in search_results:
                    markup = [[InlineKeyboardButton('Select', callback_data='getlatest=' + shorten(result['link']))]]
                    context.bot.send_photo(chat_id=user.chat_id, photo=result['thumbnail'], caption=result['title'],
                                           timeout=5, reply_markup=InlineKeyboardMarkup(markup))
                    # update last command
            user.update_last_command('')
        except Exception as err:
            log_error(err)

    elif last_command == 'broadcast':
        if user.is_admin():
            context.bot.send_message(chat_id=user.chat_id, text='Broadcasting message...')
            try:
                results = client.query(
                    q.paginate(q.documents(q.collection(users)), size=100000)
                )
                results = results['data']
                pprint(results)
                pprint(len(results))

                # spin 5 processes
                with Pool(5) as p:
                    res = p.map(send_broadcast, [[int(user_ref.id()), message] for user_ref in results])
                    successful_broadcast = []
                    for i in res:
                        if i == 'success':
                            successful_broadcast.append(i)
                    logger.info('Message broadcast to ' + str(len(successful_broadcast)) + ' users')
                    print(res)
                # update user last command
                user.update_last_command('')
            except Exception as err:
                log_error(err)
        else:
            context.bot.send_message(chat_id=user.chat_id, text="Only admins can use this command!")
    else:
        context.bot.send_message(chat_id=user.chat_id,
                                 text="Sorry, I do not understand what you mean.\nPlease use the /help command to "
                                      "discover what I can help you with.")


@mark_inactive(message=maintenance_message)
def callback_handler_func(update: Update, context: CallbackContext):
    user = User(update.effective_chat.id)
    callback_message = update.callback_query.message.reply_markup.inline_keyboard[0][0].callback_data

    [command, payload] = callback_message.split(sep='=')

    if command == 'subscribe':
        user.subscribe_to_anime(payload)
    elif command == 'unsubscribe':
        user.unsubscribe_from_anime(payload)

    # TODO: test this
    elif command == 'getlatest':
        try:
            anime_info = scraper.get_anime_info(payload)

            latest_episode_download_link = shorten(scraper.get_download_link(anime_info['latest_episode_link']))
            markup = [[InlineKeyboardButton(text='Download', url=latest_episode_download_link)]]
            context.bot.send_message(chat_id=user.chat_id, text=anime_info['latest_episode_title'],
                                     reply_markup=InlineKeyboardMarkup(markup))
        except CannotDownloadAnimeException as err:
            log_error(err)
            context.bot.send_message(chat_id=user.chat_id, text="Sorry," + payload + "could not be downloaded at this "
                                                                                     "time!")
            context.bot.send_message(chat_id=os.getenv('ADMIN_CHAT_ID'), text='A user tried to download ' + payload +
                                                                              "but could not due to error: " + str(err))
            return
        except Exception as err:
            log_error(err)
            return
        else:
            # check if anime is in our anime registry
            try:
                anime_from_db = client.query(
                    q.if_(
                        q.is_null(q.get(q.match(q.index(anime_by_id), anime_info['anime_id']))),
                        None,
                        q.let(
                            {
                                'anime': q.get(q.match(q.index(anime_by_id), anime_info['anime_id']))
                            },
                            q.if_(
                                q.gt(anime_info['number_of_episodes'], q.select(['data', 'episodes'], q.var('anime'))),
                                q.var('anime'),
                                None
                            )
                        )
                    )
                )
            except errors.NotFound:
                anime_from_db = None
            if anime_from_db is not None:
                send_update_to_subscribed_users(anime_from_db, download_link=latest_episode_download_link,
                                                anime_info=anime_info)
    else:
        pass


@mark_inactive(message=maintenance_message)
def subscribe(update, context):
    chat_id = update.effective_chat.id

    try:
        client.query(
            q.if_(
                q.exists(q.ref(q.collection(users), chat_id)),
                q.update(
                    q.ref(q.collection(users), chat_id),
                    {
                        'data': {
                            'last_command': 'subscribe'
                        }
                    }
                ),
                q.create(
                    q.ref(q.collection(users), chat_id),
                    {
                        'data': {
                            'name': update.message.chat.first_name,
                            'is_admin': False,
                            'last_command': 'subscribe',
                            'animes_watching': []
                        }
                    }
                )
            )
        )

        context.bot.send_message(chat_id=chat_id, text='Enter the anime you want to get notifications for!')
    except Exception as err:
        log_error(err)


@mark_inactive(message=maintenance_message)
def unsubscribe(update: Update, context: CallbackContext):
    user = User(update.effective_chat.id)
    # TODO: TEST THE UNSUBSCRIBE COMMAND WHEN THE USER IS NOT FOLLWOWING ANY ANIME
    try:
        animes_watched = client.query(
            q.let(
                {
                    'bot_user': q.ref(q.collection(users), user.chat_id)
                },
                q.if_(
                    q.exists(q.var('bot_user')),
                    q.map_(
                        q.lambda_('doc_ref', q.get(q.var('doc_ref'))),
                        q.select(['data', 'animes_watching'], q.get(q.var('bot_user')))
                    ),
                    []
                )
            )

        )

        for anime in animes_watched:
            markup = [[InlineKeyboardButton('Unsubscribe', callback_data='unsubscribe=' + anime['ref'].id())]]
            context.bot.send_message(chat_id=user.chat_id, text=anime['data']['title'],
                                     reply_markup=InlineKeyboardMarkup(markup))

        # update last command
        user.update_last_command('')

        if not animes_watched:
            context.bot.send_message(chat_id=user.chat_id, text='You are currently not subscribed to any anime')
    except Exception as err:
        log_error(err)


@mark_inactive(message=maintenance_message)
def get_latest(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id

    try:
        client.query(
            q.if_(
                q.exists(q.ref(q.collection(users), chat_id)),
                q.update(
                    q.ref(q.collection(users), chat_id),
                    {
                        'data': {
                            'last_command': 'getlatest'
                        }
                    }
                ),
                q.create(
                    q.ref(q.collection(users), chat_id),
                    {
                        'data': {
                            'name': update.message.chat.first_name,
                            'last_command': 'getlatest',
                            'animes_watching': []
                        }
                    }
                )
            )
        )
        context.bot.send_message(chat_id=chat_id, text='Enter the anime you want to get!')
    except Exception as err:
        log_error(err)


@mark_inactive(message=maintenance_message)
def help_user(update, context):
    user = User(update.effective_chat.id)
    if str(user.chat_id) == str(os.getenv('ADMIN_CHAT_ID')):
        message = config['message']['help_admin']
    else:
        message = config['message']['help']
    context.bot.send_message(chat_id=user.chat_id, text=message)
    try:
        client.query(
            q.let(
                {
                    'user': q.ref(q.collection(users), user.chat_id)
                },
                q.if_(
                    q.exists(q.var('user')),
                    q.update(
                        q.var('user'),
                        {
                            'data': {
                                'last_command': '',
                            }
                        }
                    ),
                    'Success!'
                )
            )
        )
    except Exception as err:
        log_error(err)


@mark_inactive(message=maintenance_message)
def donate(update, context):
    try:
        for message in config['message']['donate']:
            context.bot.send_message(chat_id=update.effective_chat.id, text=message)
        client.query(
            q.let(
                {
                    'user': q.ref(q.collection(users), update.effective_chat.id)
                },
                q.if_(
                    q.exists(q.var('user')),
                    q.update(
                        q.var('user'),
                        {
                            'data': {
                                'last_command': '',
                            }
                        }
                    ),
                    'Success!'
                )
            )
        )
    except Exception as err:
        log_error(err)


@mark_inactive(message=maintenance_message)
def error_handler(update: Update, context: CallbackContext):
    try:
        raise context.error
    except BadRequest as err:
        # handle malformed requests - read more below!
        log_error(err)
    except TimedOut as err:
        # handle slow connection problems
        log_error(err)
    except NetworkError as err:
        # handle other connection problems
        log_error(err)
    except ChatMigrated as err:
        # the chat_id of a group has changed, use e.new_chat_id instead
        log_error(err)
    except TelegramError as err:
        # handle all other telegram related errors
        log_error(err)
    except Exception as err:
        log_error(err)


@mark_inactive(message=maintenance_message)
def recommend(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    results = client.query(
        q.map_(
            q.lambda_(['followers', 'doc_ref'], q.get(q.var('doc_ref'))),
            q.paginate(q.match(q.index(sort_anime_by_followers)), size=5)
        )
    )

    context.bot.send_message(chat_id=chat_id, text='Here are the top animes people using Anime Alarm are watching')

    for anime in results['data']:
        if anime['data']['link'].startswith('https://tinyurl.com/') or anime['data']['link'].startswith(
                'https://bit.ly/'):
            link = anime['data']['link']
        else:
            link = shorten(anime['data']['link'])
        markup = [[InlineKeyboardButton('Subscribe', callback_data='subscribe=' + link)]]
        context.bot.send_message(chat_id=chat_id, reply_markup=InlineKeyboardMarkup(markup),
                                 text=str(results['data'].index(anime) + 1) + '. ' + anime['data']['title'])


@mark_inactive(message=maintenance_message)
@admin_only
def number_of_users(update: Update, context: CallbackContext):
    user = User(update.effective_chat.id)
    result = client.query(
        q.count(
            q.paginate(
                q.documents(
                    q.collection(users)
                ),
                size=100000

            ),

        )
    )
    context.bot.send_message(chat_id=user.chat_id, text='Number of users: ' + str(result['data'][0]))


@mark_inactive(message=maintenance_message)
@admin_only
def number_of_anime(update: Update, context: CallbackContext):
    result = client.query(
        q.count(
            q.paginate(
                q.documents(
                    q.collection(animes)
                ),
                size=100000
            )
        )
    )
    context.bot.send_message(chat_id=update.effective_chat.id, text='Number of anime: ' + str(result['data'][0]))


@mark_inactive(message=maintenance_message)
@admin_only
def broadcast(update: Update, context: CallbackContext):
    user = User(update.effective_chat.id)
    context.bot.send_message(chat_id=user.chat_id, text='Enter the message you want to broadcast')
    user.update_last_command('broadcast')


@mark_inactive(message=maintenance_message)
@admin_only
def app_log(update: Update, context: CallbackContext):
    user = User(update.effective_chat.id)
    logs = []
    with open(log_file_path, 'r') as f:
        logs = f.readlines()
        context.bot.send_message(chat_id=user.chat_id, text=''.join(logs[-5:]))


watch_handler = CommandHandler('subscribe', subscribe, run_async=True)
unwatch_handler = CommandHandler('unsubscribe', unsubscribe, run_async=True)
help_handler = CommandHandler(['help', 'start'], help_user, run_async=True)
donate_handler = CommandHandler('donate', donate, run_async=True)
message_handler = MessageHandler(Filters.text & (~Filters.command), plain_message, run_async=True)
callback_handler = CallbackQueryHandler(callback_handler_func, run_async=True)
get_latest_handler = CommandHandler('latest', get_latest, run_async=True)
recommend_handler = CommandHandler('recommend', recommend, run_async=True)
users_handler = CommandHandler('usercount', number_of_users, run_async=True)
anime_handler = CommandHandler('animecount', number_of_anime, run_async=True)
broadcast_handler = CommandHandler('broadcast', broadcast, run_async=True)
app_log_handler = CommandHandler('log', app_log, run_async=True)

dispatcher.add_handler(watch_handler)
dispatcher.add_handler(unwatch_handler)
dispatcher.add_handler(help_handler)
dispatcher.add_handler(donate_handler)
dispatcher.add_handler(message_handler)
dispatcher.add_handler(callback_handler)
dispatcher.add_handler(get_latest_handler)
dispatcher.add_handler(recommend_handler)
dispatcher.add_handler(users_handler)
dispatcher.add_handler(anime_handler)
dispatcher.add_handler(broadcast_handler)
dispatcher.add_handler(app_log_handler)

dispatcher.add_error_handler(error_handler)

if __name__ == '__main__':
    print('started')
    #updater.start_polling()
    updater.start_webhook(
        listen='0.0.0.1',
        port=8443,
        url_path=os.getenv('TELEGRAM_TOKEN'),
        webhook_url='https://acb599a054d2.ngrok.io/'+os.getenv('TELEGRAM_TOKEN')
    )
    run_cron()