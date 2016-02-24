import logging
import datetime

from pajbot.modules import BaseModule, ModuleSetting
from pajbot.models.command import Command
from pajbot.models.handler import HandlerManager
from pajbot.managers.redis import RedisManager
from pajbot.streamhelper import StreamHelper

import requests
from apscheduler.schedulers.background import BackgroundScheduler

log = logging.getLogger(__name__)

class HSBetModule(BaseModule):

    ID = __name__.split('.')[-1]
    NAME = 'Hearthstone Betting'
    DESCRIPTION = 'Enables betting on Hearthstone game outcomes with !hsbet'
    SETTINGS = [
            ModuleSetting(
                key='trackobot_username',
                label='Track-o-bot Username',
                type='text',
                required=True,
                placeholder='Username',
                default='',
                constraints={
                    'min_str_len': 2,
                    'max_str_len': 32,
                    }),
            ModuleSetting(
                key='trackobot_api_key',
                label='Track-o-bot API Key',
                type='text',
                required=True,
                placeholder='API Key',
                default='',
                constraints={
                    'min_str_len': 2,
                    'max_str_len': 32,
                    }),
            ModuleSetting(
                key='time_until_bet_closes',
                label='Seconds until betting closes',
                type='number',
                required=True,
                placeholder='Seconds until betting closes',
                default=120),
            ModuleSetting(
                key='max_bet',
                label='Max bet in points',
                type='number',
                required=True,
                placeholder='Max bet',
                default=5000,
                constraints={
                    'min_value': 500,
                    'max_value': 30000,
                    }),
            ]

    def __init__(self):
        super().__init__()
        self.bets = {}

        redis = RedisManager.get()

        self.last_game_start = None
        self.last_game_id = None
        try:
            last_game_start_timestamp = int(redis.get('{streamer}:last_hsbet_game_start'.format(streamer=StreamHelper.get_streamer())))
            self.last_game_start = datetime.fromtimestamp(last_game_start_timestamp)
        except (TypeError, ValueError):
            # Issue with the int-cast
            pass
        except (OverflowError, OSError):
            # Issue with datetime.fromtimestamp
            pass

        try:
            self.last_game_id = int(redis.get('{streamer}:last_hsbet_game_id'.format(streamer=StreamHelper.get_streamer())))
        except (TypeError, ValueError):
            pass

        self.scheduler = BackgroundScheduler()
        self.scheduler.start()
        self.job = self.scheduler.add_job(self.poll_trackobot, 'interval', seconds=15)
        self.job.pause()

    def poll_trackobot(self):
        url = 'https://trackobot.com/profile/history.json?username={username}&token={api_key}'.format(
                username=self.settings['trackobot_username'],
                api_key=self.settings['trackobot_api_key'])
        r = requests.get(url)
        game_data = r.json()
        if 'history' not in game_data:
            log.error('Invalid json?')
            return False

        if len(game_data['history']) == 0:
            log.error('No games found in the history.')
            return False

        self.bot.mainthread_queue.add(self.poll_trackobot_stage2,
                args=[game_data])

    def poll_trackobot_stage2(self, game_data):
        latest_game = game_data['history'][0]

        if latest_game['id'] != self.last_game_id:
            winners = []
            losers = []
            total_winning_points = 0
            total_losing_points = 0
            for username in self.bets:
                bet_for_win, points = self.bets[username]
                """
                self.bot.say('{} bet {} points on the last game to end up as a {}'.format(
                    username,
                    points,
                    'win' if bet_for_win else 'loss'))
                    """

                user = self.bot.users[username]

                correct_bet = (latest_game['result'] == 'win' and bet_for_win is True) or (latest_game['result'] == 'loss' and bet_for_win is False)
                if correct_bet:
                    winners.append((user, points))
                    total_winning_points += points
                else:
                    losers.append((user, points))
                    total_losing_points += points

            for obj in losers:
                user, points = obj
                user.pay_debt(points)
                log.debug('{} lost {} points!'.format(user, points))

            if total_losing_points > 0:
                tax = 0.0  # 1.0 = 100% tax
                total_losing_points_w_tax = int((total_losing_points - (total_losing_points * tax)))
                if total_losing_points_w_tax > 0:
                    for obj in winners:
                        points_reward = 0

                        user, points = obj
                        user.remove_debt(points)

                        if points == 0:
                            # If you didn't bet any points, you don't get a part of the cut.
                            HandlerManager.trigger('on_user_win_hs_bet', user, points_reward)
                            continue

                        pot_cut = points / total_winning_points
                        points_reward = int(pot_cut * total_losing_points)
                        user.points += points_reward
                        HandlerManager.trigger('on_user_win_hs_bet', user, points_reward)
                        self.bot.say('{} bet {} points, and made a profit of {} points by correctly betting on the HS game!'.format(
                            user.username_raw, points, points_reward))

            self.bot.say('A new game has begun! Vote with !hsbet win/lose POINTS')
            self.bets = {}
            self.last_game_id = latest_game['id']
            self.last_game_start = datetime.datetime.now() + datetime.timedelta(seconds=self.settings['time_until_bet_closes'])

            # stats about the game
            ratio = 'infinity'
            try:
                ratio = total_winning_points / total_losing_points
            except:
                pass
            self.bot.say('{0} points bet on win, {1} points bet on lose. {2}:1 win/lose ratio'.format(total_winning_points, total_losing_points, ratio))

            redis = RedisManager.get()
            redis.set('{streamer}:last_hsbet_game_id'.format(streamer=StreamHelper.get_streamer()), self.last_game_id)
            redis.set('{streamer}:last_hsbet_game_start'.format(streamer=StreamHelper.get_streamer()), self.last_game_start.timestamp())

    def command_bet(self, **options):
        bot = options['bot']
        source = options['source']
        message = options['message']

        if message is None:
            return False

        if self.last_game_start is None:
            return False

        if datetime.datetime.now() > self.last_game_start:
            bot.whisper(source.username, 'The game is too far along for you to bet on it. Wait until the next game!')
            return False

        msg_parts = message.split(' ')
        if msg_parts == 0:
            bot.whisper(source.username, 'Usage: !hsbet win/lose POINTS')
            return False

        outcome = msg_parts[0].lower()
        bet_for_win = False

        if outcome in ('win', 'winner'):
            bet_for_win = True
        elif outcome in ('lose', 'loss', 'loser'):
            bet_for_win = False
        else:
            bot.whisper(source.username, 'Invalid bet. Usage: !hsbet win/loss POINTS')
            return False

        points = 0
        try:
            points = int(msg_parts[1])
        except (IndexError, ValueError, TypeError):
            bot.whisper(source.username, 'Invalid bet. Usage: !hsbet win/loss POINTS')
            return False

        if points < 0:
            bot.whisper(source.username, 'You cannot bet negative points.')
            return False

        if points > self.settings['max_bet']:
            bot.whisper(source.username, 'You cannot bet more than {} points, please try again!'.format(self.settings['max_bet']))
            return False

        if not source.can_afford(points):
            bot.whisper(source.username, 'You don\'t have {} points to bet'.format(points))
            return False

        if source.username in self.bets:
            bot.whisper(source.username, 'You have already bet on this game. Wait until the next game starts!')
            return False

        source.create_debt(points)
        self.bets[source.username] = (bet_for_win, points)
        bot.whisper(source.username, 'You have bet {} points on this game resulting in a {}.'.format(points, 'win' if bet_for_win else 'loss'))

    def command_open(self, **options):
        bot = options['bot']
        message = options['message']

        time_limit = self.settings['time_until_bet_closes']

        if message:
            msg_split = message.split(' ')
            try:
                time_limit = int(msg_split[0])

                if time_limit < 10:
                    time_limit = 10
                elif time_limit > 180:
                    time_limit = 180
            except (ValueError, TypeError):
                pass

        self.last_game_start = datetime.datetime.now() + datetime.timedelta(seconds=time_limit)

        bot.say('The bet for the current hearthstone game is open again! You have {} seconds to vote !hsbet win/lose POINTS'.format(time_limit))

    def load_commands(self, **options):
        self.commands['hsbet'] = Command.multiaction_command(
                level=100,
                default='bet',
                fallback='bet',
                delay_all=0,
                delay_user=0,
                commands={
                    'bet': Command.raw_command(
                        self.command_bet,
                        delay_all=0,
                        delay_user=10,
                        ),
                    'open': Command.raw_command(
                        self.command_open,
                        level=500,
                        delay_all=0,
                        delay_user=0)
                    })

    def enable(self, bot):
        if bot:
            self.job.resume()
        self.bot = bot

    def disable(self, bot):
        if bot:
            self.job.pause()