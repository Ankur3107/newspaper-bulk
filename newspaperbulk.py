import pandas as pd
import numpy as np
import argparse
import pathlib
import time
import csv
import os 

import newspaper
from threading import Thread
from queue import Queue

import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from requests.exceptions import ConnectionError, InvalidSchema, MissingSchema, TooManyRedirects, RetryError

## for date parsing
from bs4 import BeautifulSoup
from dateutil.parser import parse


def parse_input_file(filepath):
    """
    Accepts either relative paths from the current directory or absolute paths
    from elsewhere in .csv, .txt, .xlsx, or .xls format. Errors caused by column
    headers are handled and passed (will print a MissingSchema error to 
    the console). URLs must be in the first column of the file. 
    """

    abspath = os.path.abspath(filepath)

    _, ext = os.path.splitext(abspath)
    name = os.path.basename(abspath).split('.')[0]
    
    if ext == '.xlsx' or ext == '.xls':
        df = pd.read_excel(abspath, header=None, columns='A')
        urls = list(df.iloc[:, 0])
    
    elif ext == '.csv' or ext == '.txt':
        urls = open(abspath, 'r').read().splitlines()
       
    else:
        raise Exception("File must in be .csv, .txt, .xlsx, or .xls format.")
   
    return urls, name


def clean_up_output(filename):
    """
    Once Newspaper is done scraping, read the file back in, drop missing
    observations in column 'text' and return the count of valid articles 
    retrieved. Then export a cleaned-up version of the file without any of the 
    blanks in the 'text' column.
    """
    
    df = pd.read_csv(filename).dropna(subset=['text'])
    
    df.to_csv(filename, index=False)
    
    return len(df)


def create_output_filename(name):
    """
    The output file will go in the 'exports' sub-directory, saved as the
    [name] with '-contents.csv' appended. PurePosixPath should ensure functionality 
    on both Windows and Linux (see https://docs.python.org/3/library/pathlib.html).
    A separate file ([name] + '-error.csv') will be created to log error URLs.
    """

    path = pathlib.PurePath(os.getcwd())
    
    output_name_clean = str(path / 'exports' / (name + "-contents.csv"))
    output_name_error = str(path / 'exports' / (name + "-error.csv"))

    return output_name_clean, output_name_error


def create_session(max_retries=0, backoff_factor=0):
    
    session = requests.Session()
    
    # See https://stackoverflow.com/questions/15431044/can-i-set-max-retries-for-requests-request/#35504626
    retries = Retry(
        total=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=[500, 502, 503, 504]
    )
    
    adapter = HTTPAdapter(max_retries=retries)
    
    session.mount('http://',  adapter)
    session.mount('https://', adapter)
    
    return session

def get_date_time(html):
    soup = BeautifulSoup(html)
    time_tag = soup.find('time')
    time_tag = time_tag.getText()[14:-4]
    dt = parse(time_tag)
    return str(dt.date()), str(dt.time())

def get_text_from_url(url, session, cleanwriter, errorwriter, allow_redirects=False, verify=True):

    url_idx = url[0]
    url_str = url[1]
    
    try: 
        response = session.get(url_str, allow_redirects=allow_redirects, verify=verify)
        response.close()

    except (ConnectionError, InvalidSchema) as e:
        errorwriter.writerow([url_str, e.__class__.__name__])
        response = None
        
        print(("#%s:" % url_idx), e.__class__.__name__, url_str)

        pass 
        
    except (MissingSchema, TooManyRedirects, RetryError) as e:
        errorwriter.writerow([url_str, e.__class__.__name__])
        response = None
        
        print(("#%s:" % url_idx), e.__class__.__name__, url_str)
        
        pass 
    
    if response is not None:
        if response.ok:       
            article = newspaper.Article(url_str)
            article.download()

            # See https://github.com/codelucas/newspaper/blob/master/newspaper/article.py#L31
            if article.download_state == 2:
                article.parse()
                article.nlp()
                date, time = get_date_time(article.html)
                cleanwriter.writerow([
                    article.text,
                    article.title,
                    article.keywords,
                    url_str,
                    article.tags,
                    article.meta_keywords,
                    date,
                    time
                ])
            
        else:   
            errorwriter.writerow([url_str, response.status_code])
            print("#%s: Error with status code %s for URL: %s"
                  % (url_idx, response.status_code, url_str))
            
    else:
        print("%s is not a valid URL" % url_str)


def target_task(q, session, cleanwriter, errorwriter, allow_redirects=False, verify=True):
    """
    This 'target' function (the function that our threads will act on) is just to ensure
    that q.get(). get_text_from_url(), and q.task_done() are called within the same method,
    smoothly and in that order.
    """

    while not q.empty():

        url = q.get()

        get_text_from_url(
            url,
            session,
            cleanwriter,
            errorwriter,
            allow_redirects=allow_redirects,
            verify=verify
        )

        q.task_done()


def main():
    
    parser = argparse.ArgumentParser()
    
    parser.add_argument('filepath', type=str,
                        help='Enter the path of the .csv, .txt, .xlsx, or .xls file containing the URLs. \
                            If the file is not in your current directory, you must enter the absolute path.')

    parser.add_argument('-t', '--threads', type=int, default=100,
                        help='Number of threads to launch (default 100).')

    parser.add_argument('-r', '--redirects', action='store_true',
                        help='Select to allow redirects.')

    parser.add_argument('-u', '--unverified', action='store_false',
                        help='Select to allow unverified SSL certificates.')
                        
    parser.add_argument('-m', '--max_retries', type=int, default=0,
                        help='Set the max number of retries (default 0 to fail on first retry).')
                        
    parser.add_argument('-b', '--backoff', type=float, default=0,
                        help='Set the backoff factor (default 0).')
    
    args = parser.parse_args()
    
    session = create_session(args.max_retries, args.backoff)
    
    urls, name = parse_input_file(args.filepath)
    total_urls = len(urls)
    
    output_name_clean, output_name_error = create_output_filename(name)

    start_time = time.time()
    
    with open(output_name_clean, 'w', newline="", encoding='utf-8') as cleanfile, \
            open(output_name_error, 'w', newline="", encoding='utf-8') as errorfile:
    
        cleanwriter = csv.writer(cleanfile, dialect='excel')
        errorwriter = csv.writer(errorfile, dialect='excel')
        
        cleanwriter.writerow(['text', 'title', 'keywords', 'url', 'tags', 'meta_tags', 'date', 'time'])
        errorwriter.writerow(['url', 'error'])

        q = Queue(maxsize=0)

        threads = min(args.threads, total_urls)

        for i in range(total_urls):
            q.put((i, urls[i]))

        for i in range(threads):
            thread = Thread(
                target=target_task,
                args=(
                    q, session,
                    cleanwriter,
                    errorwriter,
                    args.redirects,
                    args.unverified
                )
            )

            thread.setDaemon(True)
            thread.start()

        q.join()
            
    successful_urls = clean_up_output(output_name_clean)
    success_rate = successful_urls / total_urls
    
    end_time = time.time() - start_time
    
    print('\nNewspaper scrape is complete.\n')
    
    print('A total of %s out of %s articles have been collected (%s success rate) in %s seconds.\n'
          % (successful_urls, total_urls, np.round(success_rate, decimals=2), np.round(end_time, decimals=2)))
    
    
if __name__ == '__main__':
    main()
