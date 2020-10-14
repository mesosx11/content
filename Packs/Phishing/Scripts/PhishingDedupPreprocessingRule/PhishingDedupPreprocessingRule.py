import dateutil  # type: ignore

import demistomock as demisto
from CommonServerPython import *
from CommonServerUserPython import *
import pandas as pd
from bs4 import BeautifulSoup
from sklearn.feature_extraction.text import TfidfVectorizer
from numpy import dot
from numpy.linalg import norm
from email.utils import parseaddr
import tldextract
from urllib.parse import urlparse
import re

no_fetch_extract = tldextract.TLDExtract(suffix_list_urls=None)
pd.options.mode.chained_assignment = None  # default='warn'

SIMILARITY_THRESHOLD = 0.99

EMAIL_BODY_FIELD = 'emailbody'
EMAIL_SUBJECT_FIELD = 'emailsubject'
EMAIL_HTML_FIELD = 'emailbodyhtml'
FROM_FIELD = 'emailfrom'
FROM_DOMAIN_FIELD = 'fromdomain'
MERGED_TEXT_FIELD = 'mereged_text'
MIN_TEXT_LENGTH = 50
DEFAULT_ARGS = {
    'limit': '1000',
    'incidentTypes': 'Phishing',
    'exsitingIncidentsLookback': '100 days ago',
}
FROM_POLICY_TEXT_ONLY = 'TextOnly'
FROM_POLICY_EXACT = 'Exact'
FROM_POLICY_DOMAIN = 'Domain'

FROM_POLICY = FROM_POLICY_TEXT_ONLY
URL_REGEX = r'(?:(?:https?|ftp|hxxps?):\/\/|www\[?\.\]?|ftp\[?\.\]?)(?:[-\w\d]+\[?\.\]?)+[-\w\d]+(?::\d+)?' \
            r'(?:(?:\/|\?)[-\w\d+&@#\/%=~_$?!\-:,.\(\);]*[\w\d+&@#\/%=~_$\(\);])?'


def get_existing_incidents(input_args):
    global DEFAULT_ARGS
    get_incidents_args = {}
    get_incidents_args['limit'] = input_args.get('limit', DEFAULT_ARGS['limit'])
    if 'exsitingIncidentsLookback' in input_args:
        get_incidents_args['fromDate'] = input_args['exsitingIncidentsLookback']
    elif 'exsitingIncidentsLookback' in DEFAULT_ARGS:
        get_incidents_args['fromDate'] = DEFAULT_ARGS['exsitingIncidentsLookback']
    status_scope = input_args.get('statusScope', 'All')
    query_components = []
    if 'query' in input_args:
        query_components.append(input_args['query'])
    if status_scope == 'ClosedOnly':
        query_components.append('status:closed')
    elif status_scope == 'NonClosedOnly':
        query_components.append('-status:closed')
    elif status_scope == 'All':
        pass
    else:
        return_error('Unsupported statusScope: {}'.format(status_scope))
    incident_type = input_args.get('incidentTypes')
    if incident_type is not None:
        type_field = input_args.get('incidentTypeFieldName', 'type')
        type_query = '{}:{}'.format(type_field, incident_type)
        query_components.append(type_query)
    if len(query_components) > 0:
        get_incidents_args['query'] = ' and '.join('({})'.format(c) for c in query_components)
    incidents_query_res = demisto.executeCommand('GetIncidentsByQuery', get_incidents_args)
    if is_error(incidents_query_res):
        return_error(get_error(incidents_query_res))
    incidents = json.loads(incidents_query_res[-1]['Contents'])
    return incidents


def extract_domain(address):
    global no_fetch_extract
    if address == '':
        return ''
    email_address = parseaddr(address)[1]
    ext = no_fetch_extract(email_address)
    return ext.domain


def get_text_from_html(html):
    soup = BeautifulSoup(html)
    # kill all script and style elements
    for script in soup(["script", "style"]):
        script.extract()  # rip it out
    # get text
    text = soup.get_text()
    # break into lines and remove leading and trailing space on each
    lines = (line.strip() for line in text.splitlines())
    # break multi-headlines into a line each
    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
    # drop blank lines
    text = '\n'.join(chunk for chunk in chunks if chunk)
    return text


def eliminate_urls_extensions(text):
    urls_list = re.findall(URL_REGEX, text)
    for url in urls_list:
        parsed_uri = urlparse(url)
        url_with_no_path = '{uri.scheme}://{uri.netloc}/'.format(uri=parsed_uri)
        text = text.replace(url, url_with_no_path)
    return text


def preprocess_text_fields(incident):
    email_body = email_subject = email_html = ''
    if EMAIL_BODY_FIELD in incident:
        email_body = incident[EMAIL_BODY_FIELD]
    if EMAIL_HTML_FIELD in incident:
        email_html = incident[EMAIL_HTML_FIELD]
    if EMAIL_SUBJECT_FIELD in incident:
        email_subject = incident[EMAIL_SUBJECT_FIELD]
    if isinstance(email_html, float):
        email_html = ''
    if email_body is None or isinstance(email_body, float) or email_body.strip() == '':
        email_body = get_text_from_html(email_html)
    if isinstance(email_subject, float):
        email_subject = ''
    text = eliminate_urls_extensions(email_subject + ' ' + email_body)
    return text


def preprocess_incidents_df(existing_incidents):
    global MERGED_TEXT_FIELD, FROM_FIELD, FROM_DOMAIN_FIELD
    incidents_df = pd.DataFrame(existing_incidents)
    incidents_df['CustomFields'] = incidents_df['CustomFields'].fillna(value={})
    custom_fields_df = incidents_df['CustomFields'].apply(pd.Series)
    unique_keys = [k for k in custom_fields_df if k not in incidents_df]
    custom_fields_df = custom_fields_df[unique_keys]
    incidents_df = pd.concat([incidents_df.drop('CustomFields', axis=1),
                              custom_fields_df], axis=1).reset_index()
    incidents_df[MERGED_TEXT_FIELD] = incidents_df.apply(lambda x: preprocess_text_fields(x), axis=1)
    incidents_df = incidents_df[incidents_df[MERGED_TEXT_FIELD].str.len() >= MIN_TEXT_LENGTH]
    incidents_df.reset_index(inplace=True)
    if FROM_FIELD in incidents_df:
        incidents_df[FROM_FIELD] = incidents_df[FROM_FIELD].fillna(value='')
    else:
        incidents_df[FROM_FIELD] = ''
    incidents_df[FROM_FIELD] = incidents_df[FROM_FIELD].apply(lambda x: x.strip())
    incidents_df[FROM_DOMAIN_FIELD] = incidents_df[FROM_FIELD].apply(lambda address: extract_domain(address))
    incidents_df['created'] = incidents_df['created'].apply(lambda x: dateutil.parser.parse(x))  # type: ignore
    return incidents_df


def incident_has_text_fields(incident):
    text_fields = [EMAIL_SUBJECT_FIELD, EMAIL_HTML_FIELD, EMAIL_BODY_FIELD]
    if any(field in incident for field in text_fields):
        return True
    elif 'CustomFields' in incident and any(field in incident['CustomFields'] for field in text_fields):
        return True
    return False


def filter_out_same_incident(existing_incidents_df, new_incident):
    same_id_mask = existing_incidents_df['id'] == new_incident['id']
    existing_incidents_df = existing_incidents_df[~same_id_mask]
    return existing_incidents_df


def filter_newer_incidents(existing_incidents_df, new_incident):
    new_incident_datetime = dateutil.parser.parse(new_incident['created'])  # type: ignore
    earlier_incidents_mask = existing_incidents_df['created'] < new_incident_datetime
    return existing_incidents_df[earlier_incidents_mask]


def vectorize(text, vectorizer):
    return vectorizer.transform([text]).toarray()[0]


def cosine_sim(a, b):
    return dot(a, b) / (norm(a) * norm(b))


def find_duplicate_incidents(new_incident, existing_incidents_df):
    global MERGED_TEXT_FIELD, FROM_POLICY
    new_incident_text = new_incident[MERGED_TEXT_FIELD]
    text = [new_incident_text] + existing_incidents_df[MERGED_TEXT_FIELD].tolist()
    vectorizer = TfidfVectorizer(token_pattern=r"(?u)\b\w\w+\b|!|\?|\"|\'").fit(text)
    new_incident_vector = vectorize(new_incident_text, vectorizer)
    existing_incidents_df['vector'] = existing_incidents_df[MERGED_TEXT_FIELD].apply(lambda x: vectorize(x, vectorizer))
    existing_incidents_df['similarity'] = existing_incidents_df['vector'].apply(
        lambda x: cosine_sim(x, new_incident_vector))
    if FROM_POLICY == FROM_POLICY_DOMAIN:
        mask = (existing_incidents_df[FROM_DOMAIN_FIELD] != '') & \
               (existing_incidents_df[FROM_DOMAIN_FIELD] == new_incident[FROM_DOMAIN_FIELD])
        existing_incidents_df = existing_incidents_df[mask]
    elif FROM_POLICY == FROM_POLICY_EXACT:
        mask = (existing_incidents_df[FROM_FIELD] != '') & \
               (existing_incidents_df[FROM_FIELD] == new_incident[FROM_FIELD])
        existing_incidents_df = existing_incidents_df[mask]
    existing_incidents_df.sort_values(by='similarity', inplace=True, ascending=False)
    if len(existing_incidents_df) > 0:
        return existing_incidents_df.iloc[0], existing_incidents_df.iloc[0]['similarity']
    else:
        return None, None


def close_new_incident_and_link_to_existing(new_incident, existing_incident, similarity):
    res = demisto.executeCommand("CloseInvestigationAsDuplicate", {
        'duplicateId': existing_incident['id']})

    if is_error(res):
        return_error(res)
    message = 'Duplicate incidents found: #{}, #{} with similarity of {:.1f}%. '.format(new_incident['id'],
                                                                                        existing_incident['id'],
                                                                                        similarity * 100)
    message += 'This incident will be closed and linked to {}.'.format(existing_incident['id'])
    demisto.results(message)


def create_new_incident():
    demisto.results('No duplicate incident found')


def create_new_incident_low_similarity(existing_incident, similarity):
    message = 'No duplicate incident found.\n'
    message += 'Most similar incident found is #{} with similarity of {:.1f}%.\n'.format(existing_incident['id'],
                                                                                         similarity * 100)
    message += 'The threshold for considering 2 incidents as duplicate is a similarity ' \
               'of {:.1f}%.\n'.format(SIMILARITY_THRESHOLD * 100)
    message += 'Thus these 2 incidents will not be considered as duplicate and current incident will be created.\n'
    demisto.results(message)


def create_new_incident_no_text_fields():
    text_fields = [EMAIL_BODY_FIELD, EMAIL_HTML_FIELD, EMAIL_SUBJECT_FIELD]
    message = 'No text fields were found within this incident: {}.\n'.format(','.join(text_fields))
    message += 'Incident will be created.'
    demisto.results(message)


def create_new_incident_too_short():
    demisto.results('Incident text after preprocessing is too short for deduplication. Incident will be created.')


def main():
    global EMAIL_BODY_FIELD, EMAIL_SUBJECT_FIELD, EMAIL_HTML_FIELD, FROM_FIELD, MIN_TEXT_LENGTH, FROM_POLICY
    input_args = demisto.args()
    EMAIL_BODY_FIELD = input_args.get('emailBody', EMAIL_BODY_FIELD)
    EMAIL_SUBJECT_FIELD = input_args.get('emailSubject', EMAIL_SUBJECT_FIELD)
    EMAIL_HTML_FIELD = input_args.get('emailBodyHTML', EMAIL_HTML_FIELD)
    FROM_FIELD = input_args.get('emailFrom', FROM_FIELD)
    FROM_POLICY = input_args.get('fromPolicy', FROM_POLICY)
    existing_incidents = get_existing_incidents(input_args)
    demisto.results('found {} incidents by query'.format(len(existing_incidents)))
    if len(existing_incidents) == 0:
        create_new_incident()
        return
    new_incident = demisto.incidents()[0]
    if not incident_has_text_fields(new_incident):
        create_new_incident_no_text_fields()
        return
    new_incident_df = preprocess_incidents_df([new_incident])
    if len(new_incident_df) == 0:  # len(new_incident_df)==0 means new incident is too short
        create_new_incident_too_short()
        return
    existing_incidents_df = preprocess_incidents_df(existing_incidents)
    existing_incidents_df = filter_out_same_incident(existing_incidents_df, new_incident)
    existing_incidents_df = filter_newer_incidents(existing_incidents_df, new_incident)
    if len(existing_incidents_df) == 0:
        create_new_incident()
        return
    new_incident_preprocessed = new_incident_df.iloc[0].to_dict()
    duplicate_incident_row, similarity = find_duplicate_incidents(new_incident_preprocessed,
                                                                  existing_incidents_df)
    if duplicate_incident_row is None:
        create_new_incident()
        return
    if similarity < SIMILARITY_THRESHOLD:
        create_new_incident_low_similarity(duplicate_incident_row, similarity)
    else:
        return close_new_incident_and_link_to_existing(new_incident_df.iloc[0], duplicate_incident_row, similarity)


if __name__ in ['__main__', '__builtin__', 'builtins']:
    main()
