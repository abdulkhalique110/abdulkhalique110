"""
GitHub profile stats updater (dark mode only).

Calculates repositories, contributed repositories, commits, stars, followers and
total lines of code (added / deleted) authored by you, then writes the numbers
into dark_mode.svg.

Environment variables (set by the GitHub Action):
    ACCESS_TOKEN - GitHub personal access token
    USER_NAME    - your GitHub username
"""
import datetime
import hashlib
import os
import time

import requests
from dateutil import relativedelta
from lxml import etree

# ---------------------------------------------------------------------------
# CONFIG -- edit this
# ---------------------------------------------------------------------------
BIRTHDAY = datetime.datetime(2007, 2, 12)   # 12 Feb 2007
SVG_FILE = 'dark_mode.svg'
CACHE_DIR = 'cache'
# ---------------------------------------------------------------------------

HEADERS = {'authorization': 'token ' + os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME']
API_URL = 'https://api.github.com/graphql'

QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0,
               'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}


# --------------------------------------------------------------------------- helpers
def format_plural(unit):
    return 's' if unit != 1 else ''


def daily_readme(birthday):
    """'XX years, XX months, XX days' since birthday."""
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years),
        diff.months, 'month' + format_plural(diff.months),
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def query_count(name):
    QUERY_COUNT[name] += 1


def simple_request(func_name, query, variables):
    """POST a GraphQL query; raise if it fails."""
    for attempt in range(3):
        request = requests.post(API_URL, json={'query': query, 'variables': variables},
                                headers=HEADERS, timeout=60)
        if request.status_code == 200:
            return request
        if request.status_code in (502, 503, 504) and attempt < 2:
            time.sleep(3 * (attempt + 1))
            continue
        break
    raise Exception(func_name, 'has failed with', request.status_code, request.text, QUERY_COUNT)


def cache_path():
    return os.path.join(CACHE_DIR, hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt')


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


# --------------------------------------------------------------------------- GitHub queries
def user_getter(username):
    """Returns the account node ID."""
    query_count('user_getter')
    query = '''
    query($login: String!) {
        user(login: $login) { id createdAt }
    }'''
    request = simple_request('user_getter', query, {'login': username})
    return request.json()['data']['user']['id']


def follower_getter(username):
    query_count('follower_getter')
    query = '''
    query($login: String!) {
        user(login: $login) { followers { totalCount } }
    }'''
    request = simple_request('follower_getter', query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def graph_repos_stars(count_type, owner_affiliation):
    """Total repo count ('repos') or total stars ('stars') across the given affiliations."""
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges { node { ... on Repository { nameWithOwner stargazers { totalCount } } } }
                pageInfo { endCursor hasNextPage }
            }
        }
    }'''
    cursor, stars = None, 0
    while True:
        query_count('graph_repos_stars')
        variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
        data = simple_request('graph_repos_stars', query, variables).json()['data']['user']['repositories']
        if count_type == 'repos':
            return data['totalCount']
        stars += sum(e['node']['stargazers']['totalCount'] for e in data['edges'])
        if not data['pageInfo']['hasNextPage']:
            return stars
        cursor = data['pageInfo']['endCursor']


def recursive_loc(owner, repo_name, owner_id):
    """
    Walks a repo's default-branch history 100 commits at a time and sums up
    additions / deletions / commit count for commits authored by `owner_id`.
    Returns (additions, deletions, my_commits).
    """
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            edges { node { ... on Commit { additions deletions author { user { id } } } } }
                            pageInfo { endCursor hasNextPage }
                        }
                    }
                }
            }
        }
    }'''
    additions = deletions = my_commits = 0
    cursor = None
    while True:
        query_count('recursive_loc')
        variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
        request = simple_request('recursive_loc', query, variables)
        branch = request.json()['data']['repository']['defaultBranchRef']
        if branch is None:   # empty repo
            return 0, 0, 0
        history = branch['target']['history']
        for edge in history['edges']:
            node = edge['node']
            user = (node.get('author') or {}).get('user')
            if user and user['id'] == owner_id:
                my_commits += 1
                additions += node['additions']
                deletions += node['deletions']
        if not history['pageInfo']['hasNextPage']:
            return additions, deletions, my_commits
        cursor = history['pageInfo']['endCursor']


def loc_query(owner_affiliation, owner_id):
    """Lists all repos I can access (60 per page, larger pages cause 502s) and returns cached LOC totals."""
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges { node { ... on Repository {
                    nameWithOwner
                    defaultBranchRef { target { ... on Commit { history { totalCount } } } }
                } } }
                pageInfo { endCursor hasNextPage }
            }
        }
    }'''
    edges, cursor = [], None
    while True:
        query_count('loc_query')
        variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
        data = simple_request('loc_query', query, variables).json()['data']['user']['repositories']
        edges += data['edges']
        if not data['pageInfo']['hasNextPage']:
            break
        cursor = data['pageInfo']['endCursor']
    return cache_builder(edges, owner_id)


# --------------------------------------------------------------------------- cache
def load_cache():
    """cache line format: <repo_hash> <total_commits> <my_commits> <additions> <deletions>"""
    cache = {}
    try:
        with open(cache_path(), 'r') as f:
            for line in f:
                parts = line.split()
                if len(parts) == 5:
                    cache[parts[0]] = [int(p) for p in parts[1:]]
    except FileNotFoundError:
        pass
    return cache


def save_cache(cache, order):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_path(), 'w') as f:
        for repo_hash in order:
            f.write(repo_hash + ' ' + ' '.join(str(v) for v in cache[repo_hash]) + '\n')


def cache_builder(edges, owner_id):
    """
    Re-counts LOC only for repos whose commit count changed since last run.
    Returns [additions, deletions, net, my_commits, was_fully_cached].
    """
    cache = load_cache()
    order, fully_cached = [], True

    for edge in edges:
        node = edge['node']
        repo_hash = sha(node['nameWithOwner'])
        order.append(repo_hash)
        try:
            total_commits = node['defaultBranchRef']['target']['history']['totalCount']
        except (TypeError, KeyError):   # empty repo
            cache[repo_hash] = [0, 0, 0, 0]
            continue

        old = cache.get(repo_hash)
        if old is not None and old[0] == total_commits:
            continue
        fully_cached = False
        owner, repo_name = node['nameWithOwner'].split('/')
        try:
            add, dele, mine = recursive_loc(owner, repo_name, owner_id)
            cache[repo_hash] = [total_commits, mine, add, dele]
        except Exception as error:   # keep what we have, save progress, continue
            print('Could not count', node['nameWithOwner'], '->', error)
            cache.setdefault(repo_hash, [0, 0, 0, 0])
        save_cache(cache, order + [h for h in cache if h not in order])   # incremental save

    save_cache(cache, order)
    loc_add = sum(cache[h][2] for h in order)
    loc_del = sum(cache[h][3] for h in order)
    my_commits = sum(cache[h][1] for h in order)
    return [loc_add, loc_del, loc_add - loc_del, my_commits, fully_cached]


# --------------------------------------------------------------------------- SVG
def find_and_replace(root, element_id, new_text):
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def justify_format(root, element_id, new_text, length=0):
    """Sets the value and adjusts the dots before it so the column stays aligned."""
    if isinstance(new_text, int):
        new_text = '{:,}'.format(new_text)
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_string = {0: '', 1: ' ', 2: '. '}[just_len]
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f'{element_id}_dots', dot_string)


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    tree = etree.parse(filename)
    root = tree.getroot()
    find_and_replace(root, 'age_data', age_data)
    justify_format(root, 'commit_data', commit_data, 16)
    justify_format(root, 'star_data', star_data, 10)
    justify_format(root, 'repo_data', repo_data, 4)
    justify_format(root, 'contrib_data', contrib_data)
    justify_format(root, 'follower_data', follower_data, 8)
    justify_format(root, 'loc_data', loc_data[2], 9)
    justify_format(root, 'loc_add', loc_data[0])
    justify_format(root, 'loc_del', loc_data[1], 7)
    tree.write(filename, encoding='utf-8', xml_declaration=True)


# --------------------------------------------------------------------------- timing / printing
def perf_counter(funct, *args):
    start = time.perf_counter()
    result = funct(*args)
    return result, time.perf_counter() - start


def formatter(label, difference):
    shown = '%.4f s ' % difference if difference > 1 else '%.4f ms' % (difference * 1000)
    print('{:<23}{:>12}'.format(' ' + label + ':', shown))


# --------------------------------------------------------------------------- main
if __name__ == '__main__':
    print('Calculation times:')
    owner_id, user_time = perf_counter(user_getter, USER_NAME)
    formatter('account data', user_time)

    age_data, age_time = perf_counter(daily_readme, BIRTHDAY)
    formatter('age calculation', age_time)

    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], owner_id)
    formatter('LOC (cached)' if total_loc[-1] else 'LOC (no cache)', loc_time)

    commit_data = total_loc[3]
    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    formatter('stars', star_time)
    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    formatter('repos', repo_time)
    contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    formatter('contributed repos', contrib_time)
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)
    formatter('followers', follower_time)

    loc_formatted = ['{:,}'.format(n) for n in total_loc[:3]]
    svg_overwrite(SVG_FILE, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_formatted)

    print('Total GitHub GraphQL API calls:', sum(QUERY_COUNT.values()))
    for name, count in QUERY_COUNT.items():
        print('{:<28}{:>6}'.format(' ' + name + ':', count))
