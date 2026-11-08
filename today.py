import datetime
import hashlib
import os
import time

import requests
from dateutil import relativedelta
from lxml import etree

# Configuration
BIRTHDAY = datetime.datetime(2007, 2, 12)
SVG_FILE = 'dark_mode.svg'
CACHE_DIR = 'cache'

ACCESS_TOKEN = os.environ.get('ACCESS_TOKEN')
USER_NAME = os.environ.get('USER_NAME')

if not ACCESS_TOKEN:
    raise RuntimeError('ACCESS_TOKEN environment variable is missing.')
if not USER_NAME:
    raise RuntimeError('USER_NAME environment variable is missing.')

HEADERS = {'authorization': 'token ' + ACCESS_TOKEN}
API_URL = 'https://api.github.com/graphql'
QUERY_COUNT = {
    'user_getter': 0,
    'follower_getter': 0,
    'graph_repos_stars': 0,
    'recursive_loc': 0,
    'graph_commits': 0,
    'loc_query': 0
}


def format_plural(unit):
    return 's' if unit != 1 else ''


def daily_readme(birthday):
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years),
        diff.months, 'month' + format_plural(diff.months),
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else ''
    )


def query_count(name):
    QUERY_COUNT[name] += 1


def simple_request(func_name, query, variables):
    for attempt in range(3):
        request = requests.post(
            API_URL,
            json={'query': query, 'variables': variables},
            headers=HEADERS,
            timeout=60
        )

        if request.status_code == 200:
            payload = request.json()
            if payload.get('errors'):
                raise RuntimeError(
                    '{} GraphQL error: {}'.format(func_name, payload['errors'])
                )
            return request

        if request.status_code in (502, 503, 504) and attempt < 2:
            time.sleep(3 * (attempt + 1))
            continue

        break

    raise RuntimeError(
        '{} failed with HTTP {}: {}'.format(
            func_name, request.status_code, request.text
        )
    )


def cache_path():
    return os.path.join(
        CACHE_DIR,
        hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    )


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def user_getter(username):
    query_count('user_getter')
    query = '''
    query($login: String!) {
        user(login: $login) { id createdAt }
    }'''
    request = simple_request('user_getter', query, {'login': username})
    user = request.json()['data']['user']
    if user is None:
        raise RuntimeError('GitHub user not found: ' + username)
    return user['id']


def follower_getter(username):
    query_count('follower_getter')
    query = '''
    query($login: String!) {
        user(login: $login) { followers { totalCount } }
    }'''
    request = simple_request('follower_getter', query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def graph_repos_stars(count_type, owner_affiliation):
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
        variables = {
            'owner_affiliation': owner_affiliation,
            'login': USER_NAME,
            'cursor': cursor
        }
        data = simple_request(
            'graph_repos_stars', query, variables
        ).json()['data']['user']['repositories']

        if count_type == 'repos':
            return data['totalCount']

        stars += sum(
            edge['node']['stargazers']['totalCount']
            for edge in data['edges']
        )

        if not data['pageInfo']['hasNextPage']:
            return stars

        cursor = data['pageInfo']['endCursor']


def recursive_loc(owner, repo_name, owner_id):
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
        variables = {
            'repo_name': repo_name,
            'owner': owner,
            'cursor': cursor
        }
        request = simple_request('recursive_loc', query, variables)
        repository = request.json()['data']['repository']

        if repository is None:
            raise RuntimeError('Repository not found: {}/{}'.format(owner, repo_name))

        branch = repository['defaultBranchRef']
        if branch is None:
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
        variables = {
            'owner_affiliation': owner_affiliation,
            'login': USER_NAME,
            'cursor': cursor
        }
        data = simple_request(
            'loc_query', query, variables
        ).json()['data']['user']['repositories']
        edges += data['edges']

        if not data['pageInfo']['hasNextPage']:
            break

        cursor = data['pageInfo']['endCursor']

    return cache_builder(edges, owner_id)


def load_cache():
    cache = {}
    try:
        with open(cache_path(), 'r', encoding='utf-8') as file:
            for line in file:
                parts = line.split()
                if len(parts) == 5:
                    cache[parts[0]] = [int(part) for part in parts[1:]]
    except FileNotFoundError:
        pass
    return cache


def save_cache(cache, order):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_path(), 'w', encoding='utf-8') as file:
        for repo_hash in order:
            if repo_hash in cache:
                file.write(
                    repo_hash + ' ' +
                    ' '.join(str(value) for value in cache[repo_hash]) + '\n'
                )


def cache_builder(edges, owner_id):
    cache = load_cache()
    order, fully_cached = [], True

    for edge in edges:
        node = edge['node']
        repo_hash = sha(node['nameWithOwner'])
        order.append(repo_hash)

        try:
            total_commits = node['defaultBranchRef']['target']['history']['totalCount']
        except (TypeError, KeyError):
            cache[repo_hash] = [0, 0, 0, 0]
            continue

        old = cache.get(repo_hash)
        if old is not None and old[0] == total_commits:
            continue

        fully_cached = False
        owner, repo_name = node['nameWithOwner'].split('/')

        added, deleted, mine = recursive_loc(owner, repo_name, owner_id)
        cache[repo_hash] = [total_commits, mine, added, deleted]

        save_cache(cache, order + [key for key in cache if key not in order])

    save_cache(cache, order)

    loc_add = sum(cache[key][2] for key in order)
    loc_del = sum(cache[key][3] for key in order)
    my_commits = sum(cache[key][1] for key in order)

    return [loc_add, loc_del, loc_add - loc_del, my_commits, fully_cached]


def find_and_replace(root, element_id, new_text):
    elements = root.xpath(
        ".//*[@id=$element_id]",
        element_id=element_id
    )

    if not elements:
        raise ValueError('SVG element not found: {}'.format(element_id))

    elements[0].text = str(new_text)


LINE_WIDTH = 54
BAR_COLUMN = 34


def fmt(value):
    return '{:,}'.format(value) if isinstance(value, int) else str(value)


def set_text(root, element_id, text):
    find_and_replace(root, element_id, text)


def set_dots(root, element_id, count):
    find_and_replace(
        root,
        element_id + '_dots',
        ' ' + '.' * max(1, count) + ' '
    )


def validate_svg(filename):
    tree = etree.parse(filename)
    root = tree.getroot()

    required_ids = [
        'age_data', 'age_data_dots',
        'repo_data', 'repo_data_dots',
        'contrib_data',
        'star_data', 'star_data_dots',
        'commit_data', 'commit_data_dots',
        'follower_data', 'follower_data_dots',
        'loc_data', 'loc_data_dots',
        'loc_add', 'loc_del'
    ]

    for element_id in required_ids:
        if not root.xpath(
            ".//*[@id=$element_id]",
            element_id=element_id
        ):
            raise ValueError('Required SVG element is missing: ' + element_id)

    return True


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data,
                  contrib_data, follower_data, loc_data):
    tree = etree.parse(filename)
    root = tree.getroot()

    age = str(age_data)
    repos, contrib, stars = fmt(repo_data), fmt(contrib_data), fmt(star_data)
    commits, followers = fmt(commit_data), fmt(follower_data)
    net, added, deleted = fmt(loc_data[2]), fmt(loc_data[0]), fmt(loc_data[1])

    set_text(root, 'age_data', age)
    set_dots(root, 'age_data', LINE_WIDTH - 5 - len('Uptime') - len(age))

    set_text(root, 'repo_data', repos)
    set_text(root, 'contrib_data', contrib)
    set_text(root, 'star_data', stars)
    set_dots(root, 'repo_data', BAR_COLUMN - 27 - len(repos) - len(contrib))
    set_dots(root, 'star_data', LINE_WIDTH - BAR_COLUMN - 10 - len(stars))

    set_text(root, 'commit_data', commits)
    set_text(root, 'follower_data', followers)
    set_dots(root, 'commit_data', BAR_COLUMN - 13 - len(commits))
    set_dots(root, 'follower_data', LINE_WIDTH - BAR_COLUMN - 14 - len(followers))

    set_text(root, 'loc_data', net)
    set_text(root, 'loc_add', added)
    set_text(root, 'loc_del', deleted)
    set_dots(root, 'loc_data', LINE_WIDTH - 29 - len(net) - len(added) - len(deleted))

    tree.write(filename, encoding='utf-8', xml_declaration=True)
    validate_svg(filename)
    print('Successfully updated and validated SVG: ' + filename)


def perf_counter(funct, *args):
    start = time.perf_counter()
    result = funct(*args)
    return result, time.perf_counter() - start


def formatter(label, difference):
    shown = '%.4f s ' % difference if difference > 1 else '%.4f ms' % (difference * 1000)
    print('{:<23}{:>12}'.format(' ' + label + ':', shown))


if __name__ == '__main__':
    print('Calculation times:')

    owner_id, user_time = perf_counter(user_getter, USER_NAME)
    formatter('account data', user_time)

    age_data, age_time = perf_counter(daily_readme, BIRTHDAY)
    formatter('age calculation', age_time)

    total_loc, loc_time = perf_counter(
        loc_query,
        ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'],
        owner_id
    )
    formatter('LOC (cached)' if total_loc[-1] else 'LOC (no cache)', loc_time)

    commit_data = total_loc[3]

    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    formatter('stars', star_time)

    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    formatter('repos', repo_time)

    contrib_data, contrib_time = perf_counter(
        graph_repos_stars,
        'repos',
        ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER']
    )
    formatter('contributed repos', contrib_time)

    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)
    formatter('followers', follower_time)

    loc_formatted = ['{:,}'.format(number) for number in total_loc[:3]]

    svg_overwrite(
        SVG_FILE,
        age_data,
        commit_data,
        star_data,
        repo_data,
        contrib_data,
        follower_data,
        loc_formatted
    )

    print('Total GitHub GraphQL API calls:', sum(QUERY_COUNT.values()))
    for name, count in QUERY_COUNT.items():
        print('{:<28}{:>6}'.format(' ' + name + ':', count))
