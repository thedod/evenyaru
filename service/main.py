'Evenyaru server'
# pylint: disable=import-error,invalid-name
import os
import time
import uuid
import json
import logging
import threading

import redis
import flask
import flask_socketio as io

def make_token():
    'A timestamp is easier to debug'
    import datetime
    now = datetime.datetime.now()
    return '-'.join((now.strftime('%Y%m%d-%H%M%S'), str(now.microsecond)))

app = flask.Flask(__name__, static_url_path='')
app.config['SECRET_KEY'] = 'Should be set in env'

if 'DEBUG' in os.environ:
    app.debug = True
    app.logger.setLevel(logging.DEBUG)
else:
    streamHandler = logging.StreamHandler()
    streamHandler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s: %(message)s"))
    app.logger.addHandler(streamHandler)
    app.logger.setLevel(logging.INFO)

app.logger.info('Flask started')

db = redis.from_url(os.environ.get('REDISCLOUD_URL','this is probably a kivy service'))
pubsub = db.pubsub()

# SocketIO init.
# Set this variable to "threading", "eventlet" or "gevent" to test the
# different async modes, or leave it set to None for the application to choose
# the best option based on available packages.
async_mode = 'threading'

if async_mode is None:
    try:
        import eventlet
        async_mode = 'eventlet'
    except ImportError:
        pass

    if async_mode is None:
        try:
            from gevent import monkey
            async_mode = 'gevent'
        except ImportError:
            pass

    if async_mode is None:
        async_mode = 'threading'

# monkey patching is necessary because this application uses a background
# thread
if async_mode == 'eventlet':
    import eventlet
    eventlet.monkey_patch()
elif async_mode == 'gevent':
    from gevent import monkey
    monkey.patch_all()

socketio = io.SocketIO(app)
app.logger.info("using {} mode for SocketIO".format(async_mode))

rooms = {}


def resolve(a, b):
    'Rules of the game.'
    if a[1] == b[1]:
        return None
    if (a[1], b[1]) in [('rock', 'scissors'), ('scissors', 'paper'), ('paper', 'rock')]:
        return int(a[0])
    return int(b[0])


def publishscore(room):
    'Tell connected clients the score.'
    score = {0: '0', 1: '0'}
    score.update({
        idx: db.get("score-{}-{}".format(room, idx))
        for idx in db.lrange("teams-{}".format(room), 0, -1)})
    db.publish(room, json.dumps({'score': score}))


def checkredis():
    'Check redis for events.'
    while True:
        time.sleep(1)
        while True:
            try:
                message = pubsub.get_message()
            except AttributeError:
                break
            if message is None:
                break
            if isinstance(message['data'], long):
                break

            app.logger.debug("got msg: %s", message)
            room = message['channel']
            data = json.loads(message['data'])
            if 'score' in data.keys():
                socketio.emit('score', {'score': data['score']}, room=room)
            elif 'move' in data.keys():
                socketio.emit('move', {'move': data['move']}, room=room)
            elif 'winner' in data.keys():
                app.logger.info("win message: %s", message)
                socketio.emit('winner', {'winner': data['winner']}, room=room)
            else:
                app.logger.warning("unknown message: %s", message)

threading.Thread(target=checkredis).start()


@app.route('/')
def index():
    'Handle token registration and redirect to app.'
    response = app.send_static_file('index.html')
    return response

@app.route('/status')
def status():
    'show room status'
    return flask.render_template('status.html', rooms=rooms)

@socketio.on('hello')
def hello(message):
    'Use client-supplied token, or return something the client should store'
    token = message.get('token', make_token())
    flask.session['token'] = token
    app.logger.info("Client {} connected".format(token))
    io.emit('connected', {'token': token})

@socketio.on('join')
def join(message):
    'Joins redis channel, socketio room and game team.'
    room = message['room']
    numplayerskey = "players-{}".format(room)
    numplayers = db.incr(numplayerskey)
    if numplayers > 2:
        db.decr(numplayerskey)
        io.emit('fail', {'room': room, 'type': 'room is full'})
        return
    token = flask.session['token']
    tokenteamkey = "team-{}".format(token)
    roomteamskey = "teams-{}".format(room)
    if numplayers > 1:
        team = int(message.get('team',
            1 - int(db.lrange(roomteamskey, 0, 1)[0])))
        if team != int(db.get(tokenteamkey) or team):
            db.decr(numplayerskey)
            io.emit('fail', {'room': room, 'type': 'wrong team'})
            return
    else:
        team = int(message.get('team',
            (db.get(tokenteamkey) or 0)))

    db.lpush(roomteamskey, team)
    db.set(tokenteamkey, team)
    flask.session['room'] = room
    flask.session['team'] = team

    pubsub.subscribe(room)
    io.join_room(room)
    io.emit('ready', {'room': room, 'team': team})
    app.logger.info("Client {} joined {} as {}".format(token, room, team))

    try:
        rooms[room] += 1
    except KeyError:
        rooms[room] = 1
    publishscore(room)


@socketio.on('disconnect')
def disconnect():
    'Cleanup.'
    app.logger.info("Client {} disconnected".format(flask.session['token']))
    if 'room' in flask.session.keys():
        room = flask.session['room']
        team = flask.session['team']
        db.decr("players-{}".format(room))
        db.lrem("teams-{}".format(room), team, 0)
        del flask.session['room']

        try:
            rooms[room] -= 1
            if rooms[room] < 1:
                pubsub.unsubscribe(room)
                del rooms[room]
        except KeyError:
            pass


@socketio.on('play')
def play(message):
    'Choose a move.'
    room = flask.session.get('room')
    team = int(flask.session.get('team'))
    choice = message['choice']
    existingchoice = db.rpop(room)
    if existingchoice is not None:
        existingchoice = json.loads(existingchoice)
    if existingchoice is None or team == existingchoice[0]:
        # when client disco/reco-nects, don't play against self :)
        db.lpush(room, json.dumps((team, message['choice'])))
        db.publish(room, json.dumps({'move': team}))
        app.logger.info("Team {} played {} in {}".format(team, choice, room))
    else:
        winner = resolve(existingchoice, (team, choice))
        db.publish(room, json.dumps({'winner': winner}))
        if winner is None:
            for player in [0, 1]:
                db.incrby("score-{}-{}".format(room, player), 2)
        else:
            db.incr("score-{}-{}".format(room, winner))
        publishscore(room)
        app.logger.info("Team {} played {} against {} in {}, the winner is {}".format(
            team, choice, existingchoice[1], room, winner))


@socketio.on('log_email')
def log_email(address):
    'Send a message to log.'
    app.logger.info(
        "Client %s in team %s in room %s gave address '%s'",
        flask.session.get('token'),
        flask.session.get('team'),
        flask.session.get('room'),
        address)


if __name__ == '__main__':
    try:
        socketio.run(app, host='0.0.0.0')
    finally:
        for ROOM, NUMPLAYERS in rooms.iteritems():
            for _ in range(NUMPLAYERS):
                db.decr("players-{}".format(ROOM))
