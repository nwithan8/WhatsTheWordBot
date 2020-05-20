#!/usr/bin/python3

import os
import praw
import logging
import sql_library as sql
from datetime import datetime, timezone

# Get at https://www.reddit.com/prefs/apps
REDDIT_CLIENT_ID = ''
REDDIT_CLIENT_SECRET = ''
REDDIT_USERNAME = 'WhatsTheWordBot'
REDDIT_PASSWORD = ''

# Careful as you release this into the wild!
SUB_TO_MONITOR = "whatstheword"

SECONDS_UNTIL_ABANDONED_FROM_UNSOLVED = 86400  # 86400 = 24 hours in seconds
SECONDS_UNTIL_UNKNOWN_FROM_CONTESTED = 172800  # 172800 = 48 hours in seconds

UNSOLVED_FLAIR_TEXT = 'unsolved'
UNSOLVED_FLAIR_ID = 'fb4b2e7e-94bd-11ea-8cfc-0efa6da03c0b'
UNSOLVED_DB = 'u'
ABANDONDED_FLAIR_TEXT = 'abandoned'
ABANDONDED_FLAIR_ID = 'be422f68-9554-11ea-8bea-0e6f109dbcd3'
ABANDONDED_DB = 'a'
CONTESTED_FLAIR_TEXT = 'contested'
CONTESTED_FLAIR_ID = '012e7792-94be-11ea-9937-0ed4891340c7'
CONTESTED_DB = 'c'
SOLVED_FLAIR_TEXT = 'solved'
SOLVED_FLAIR_ID = 'f4b475ca-94bd-11ea-a3be-0e2ff1668461'
SOLVED_DB = 's'
UNKNOWN_FLAIR_TEXT = 'unknown'
UNKNOWN_FLAIR_ID = ''
UNKNOWN_DB = 'k'

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")

db = sql.SQL(sql_type='SQLite', sqlite_file='whats_the_word.db')

reddit = praw.Reddit(client_id=REDDIT_CLIENT_ID, client_secret=REDDIT_CLIENT_SECRET,
                     user_agent='WhatsTheWordBot (by u/grtgbln)', username=REDDIT_USERNAME, password=REDDIT_PASSWORD)
if not reddit.read_only:
    logging.info("Connected and running.")


def get_posts_with_old_timestamps(status, second_limit=86400):
    old_timestamp = datetime.now().replace(tzinfo=timezone.utc).timestamp() - second_limit
    # print(old_timestamp)
    results = db.custom_query(
        queries=[f"SELECT id, status FROM posts WHERE last_checked <= {int(old_timestamp)} AND status == '{status}'"])
    # print(results)
    if results and len(results) > 0:
        return results
    return []


def check_status_in_db(submission_id):
    results = db.custom_query(queries=[f"SELECT id, status FROM posts WHERE id == '{submission_id}'"])
    if results and len(results) > 0:
        return results
    return None


def check_flair(submission, flair_text, flair_id=None):
    try:
        if submission.link_flair_text == flair_text or submission.link_flair_template_id == flair_id:
            return True
        return False
    except Exception as e:
        logging.error(f"Could not check submission {submission.id} flair. {e}")
        return False


def apply_flair(submission, text="", flair_id=None):
    try:
        submission.mod.flair(text=text, flair_template_id=flair_id)
        logging.info(f"Marked submission {submission.id} as '{text}'")
        return True
    except Exception as e:
        logging.error(f"Could not apply {text} flair. {e}")
        return False


def solved_in_comment(comment):
    if "solved" in comment.body.lower():
        return True
    return False


def solved_in_comments(submission):
    # look for "solved" comment by OP
    submission.comments.replace_more(limit=None)
    for comment in submission.comments.list():
        if comment.author.name == submission.author.name and solved_in_comments(submission):
            return True
    return False


def already_solved(submission):
    return check_flair(submission=submission, flair_text=SOLVED_FLAIR_TEXT, flair_id=SOLVED_FLAIR_ID)


def already_contested(submission):
    return check_flair(submission=submission, flair_text=CONTESTED_FLAIR_TEXT, flair_id=CONTESTED_FLAIR_ID)


def store_entry_in_db(submission):
    timestamp = datetime.now().replace(tzinfo=timezone.utc).timestamp()
    try:
        results = db.custom_query(
            queries=[
                f"INSERT INTO posts (id, status, last_checked) VALUES ('{str(submission.id)}', '{UNSOLVED_DB}', {int(timestamp)})"],
            commit=True)
        if results and results > 0:
            logging.info(f"Added submission {submission.id} to database.")
            return True
        return False
    except Exception as e:
        # most likely issue is not unique (submission is already logged in databaase); this is fine and intended
        logging.error(f"Couldn't store submission in database. {e}")
        return False


def update_db_entry(submission_id, status):
    try:
        time_now = datetime.now().replace(tzinfo=timezone.utc).timestamp()
        results = db.custom_query(
            queries=[
                f"UPDATE posts SET status = '{status}', last_checked = {int(time_now)} WHERE id = '{submission_id}'"],
            commit=True)
        if results and results > 0:
            logging.info(f"Updated submission {submission_id} to '{status}' in database.")
            return True
        return False
    except Exception as e:
        logging.error(f"Couldn't update submission {submission_id} in database. {e}")
        return False


def delete_old_entry(submission_id):
    try:
        results = db.custom_query(
            queries=[
                f"DELETE FROM posts WHERE id = '{submission_id}'"],
            commit=True)
        if results and results > 0:
            logging.info(f"Deleted submission {submission_id} from database.")
            return True
        return False
    except Exception as e:
        logging.error(f"Couldn't delete submission {submission_id} in database. {e}")
        return False


def clean_db():
    results = db.custom_query(queries=['DELETE FROM posts'], commit=True)
    if results >= 0:
        logging.info('Database cleared.')
        return True
    logging.error('Database could not be cleared.')
    return False


def run():
    """
    New submission: automatically flaired "unsolved"
    If "solved" comment from OP -> "solved"
    If non-"solved" comment from OP -> "contested"
    If new comment from non-OP -> "unsolved"/"contested"/"unknown" -> "contested" (ignore "abandoned")
    After 24 hours, "unsolved" -> "abandoned" (check if solved first) (unsolved means no new comments; otherwise would be "contested")
    After 48 hours, "contested" -> "unknown" (check if solved first) (contested means someone has commented)
    """
    # clean_db()
    subreddit = reddit.subreddit(SUB_TO_MONITOR)
    # comment_stream = subreddit.stream.comments(pause_after=-1)
    # submission_stream = subreddit.stream.submissions(pause_after=-1)
    while True:
        # log new submissions to database, apply "unsolved" flair
        submission_stream = subreddit.new(
            limit=10)  # if you're getting more than 10 new submissions in two seconds, you have a problem
        for submission in submission_stream:
            if submission is None:
                break
            else:
                # only update flair if successfully added to database, to avoid out-of-sync issues
                if not check_flair(submission=submission, flair_text=UNSOLVED_FLAIR_TEXT, flair_id=UNSOLVED_FLAIR_ID) and store_entry_in_db(submission=submission):
                    apply_flair(submission, text=UNSOLVED_FLAIR_TEXT, flair_id=UNSOLVED_FLAIR_ID)
        # check if any new comments, update submissions accordingly
        comment_stream = subreddit.comments(limit=50)
        for comment in comment_stream:
            if comment is None or not comment.author or (comment.author and comment.author.name == 'AutoModerator'):
                break
            # if new comment by OP
            if comment.author and comment.author.name == comment.submission.author.name:
                # if OP's comment is "solved", flair submission as "solved"
                if not already_solved(comment.submission) and solved_in_comment(comment):
                    try:
                        # only update flair if successfully updated in database, to avoid out-of-sync issues
                        if update_db_entry(submission_id=comment.submission.id, status=SOLVED_DB):
                            apply_flair(submission=comment.submission, text=SOLVED_FLAIR_TEXT, flair_id=SOLVED_FLAIR_ID)
                    except Exception as e:
                        logging.error(
                            f"Couldn't flair submission {comment.submission.id} as 'solved' following OP's new comment.")
                # if OP's comment is not "solved", flair submission as "contested"
                elif not already_contested(comment.submission):
                    try:
                        # only update flair if successfully updated in database, to avoid out-of-sync issues
                        if update_db_entry(submission_id=comment.submission.id, status=CONTESTED_DB):
                            apply_flair(submission=comment.submission, text=CONTESTED_FLAIR_TEXT,
                                        flair_id=CONTESTED_FLAIR_ID)
                    except Exception as e:
                        logging.error(
                            f"Couldn't flair submission {comment.submission.id} as 'contested' following OP's new comment.")
            # otherwise, if new non-OP comment on an "unknown", "contested" or "unsolved" submission, flair submission as "contested"
            else:
                try:
                    submission_entry_in_db = check_status_in_db(submission_id=comment.submission.id)
                    if submission_entry_in_db and submission_entry_in_db[0][1] in [UNKNOWN_DB, CONTESTED_DB, UNSOLVED_DB]\
                            and not (
                                check_flair(submission=comment.submission, flair_text=UNKNOWN_FLAIR_TEXT, flair_id=UNKNOWN_FLAIR_ID) or
                                check_flair(submission=comment.submission, flair_text=UNSOLVED_FLAIR_TEXT, flair_id=UNSOLVED_FLAIR_ID) or
                                check_flair(submission=comment.submission, flair_text=CONTESTED_FLAIR_TEXT, flair_id=CONTESTED_FLAIR_ID)
                            ):
                        try:
                            # only update flair if successfully updated in database, to avoid out-of-sync issues
                            if update_db_entry(submission_id=comment.submission.id, status=CONTESTED_DB):
                                apply_flair(submission=comment.submission, text=CONTESTED_FLAIR_TEXT,
                                            flair_id=CONTESTED_FLAIR_ID)
                        except Exception as e:
                            logging.error(
                                f"Couldn't flair submission {comment.submission.id} as 'contested' following a new non-OP comment.")
                except Exception as e:
                    logging.error(f"Couldn't grab submmision {comment.submission.id} status from database.")
        # check old "unsolved" submissions and change to "abandoned"
        old_unsolved_submissions = get_posts_with_old_timestamps(status='u',
                                                                 second_limit=SECONDS_UNTIL_ABANDONED_FROM_UNSOLVED)
        for entry in old_unsolved_submissions:
            try:
                # get submission object from id
                submission = reddit.submission(id=entry[0])
                # check comments one last time for potential solve
                # only update flair if successfully updated in database, to avoid out-of-sync issues
                if solved_in_comments(submission=submission) or check_flair(submission=submission,
                                                                            flair_text=SOLVED_FLAIR_TEXT,
                                                                            flair_id=SOLVED_FLAIR_ID):
                    if update_db_entry(submission_id=entry[0], status=SOLVED_DB):
                        apply_flair(submission=submission, text=SOLVED_FLAIR_TEXT, flair_id=SOLVED_FLAIR_ID)
                else:
                    if update_db_entry(submission_id=entry[0], status=ABANDONDED_DB):
                        apply_flair(submission=submission, text=ABANDONDED_FLAIR_TEXT, flair_id=ABANDONDED_FLAIR_ID)
            except Exception as e:
                logging.error(f"Couldn't check old submission {entry[0]}. {e}")
                # if '404' in e:
                #    delete_old_entry(submission_id=entry[0])
        # check old "contested" submissions and change to "unknown"
        old_contested_submissions = get_posts_with_old_timestamps(status='c',
                                                                  second_limit=SECONDS_UNTIL_UNKNOWN_FROM_CONTESTED)
        for entry in old_contested_submissions:
            try:
                # get submission object from id
                submission = reddit.submission(id=entry[0])
                # check comments one last time for potential solve
                # only update flair if successfully updated in database, to avoid out-of-sync issues
                if solved_in_comments(submission=submission) or check_flair(submission=submission,
                                                                            flair_text=SOLVED_FLAIR_TEXT,
                                                                            flair_id=SOLVED_FLAIR_ID):
                    if update_db_entry(submission_id=entry[0], status=SOLVED_DB):
                        apply_flair(submission=submission, text=SOLVED_FLAIR_TEXT, flair_id=SOLVED_FLAIR_ID)
                else:
                    if update_db_entry(submission_id=entry[0], status=UNKNOWN_DB):
                        apply_flair(submission=submission, text=UNKNOWN_FLAIR_TEXT, flair_id=UNKNOWN_FLAIR_ID)
            except Exception as e:
                logging.error(f"Couldn't check old submission {entry[0]}. {e}")
                # if '404' in e:
                #    delete_old_entry(submission_id=entry[0])


run()
