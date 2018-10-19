from fcntl import fcntl, F_GETFL, F_SETFL
from os import O_NONBLOCK, read
import boto3
import json
import requests
import shlex
import subprocess
import time
import yaml
import sys
import os

MUTE_SLACK = int(sys.argv[-1].rstrip())

#Can be changed to whatever desired healthy instance count
Desired_health_count = 3

def is_command(command):
    """Determines if the input string is a command line prompt or a description.

    @param command: The string from a line of the yaml file.
    """
    cmd = command.strip()
    if '~' == cmd[0:1]:
        return True
    else :
        return False

def exiting(skip_first, skip_error, num_error):
    """Determines if 1.) Continue the current step or cancel the step
                     2.) If we are canceling the current step, should we skip
                         to the next step or terminate the process

    @param skip_first: (True) When we are manually deciding each step to run
                        or cancel
    @param skip_error: (True) When we are going to ignore all the errors and
                        keep running
    @param num_error: The number of errors that have occured so far(does not
                        consider health check errors)

    @return True: skip to next step (when there is error in current step)
    @return False: continue running
    terminates script if otherwise
    """
    if skip_error:
        return True
    answer1 = 'n'

    if not skip_first:
        answer1 = input("Press [y] to continue or [n] to cancel this step: ")
        if answer1.lower().startswith('y'):
            return False

    if answer1.lower().startswith('n'):
        answer2 = input("Press [y] to skip to next step or [n] to terminate entire process: ")
        if answer2.lower().startswith('n'):
            print('Exiting gracefully... \nTotal ' + str(num_error) + ' errors')
            post_to_slack('Exiting gracefully...\nTotal '+ str(num_error) \
                            + ' errors')
            sys.exit(1)
        else:
            print("Skipping this step...")
            post_to_slack("Skipping this step...")
            return True
    else:
        print("Answer again")
        exiting(skip_first, skip_error, num_error)

def run_command(command, file_name, skip_error, num_error):
    """Runs the command

    @param command: The command that is going to be run
    @param file_name: The name of the current file that we are reading the commands from
    @param skip_error: (True) When we are going to skip all the errors that
                        occurred and continue running the script
    @param num_error: The number of errors that have happened so far

    @return: cumulative number of errors
    """
    process = subprocess.Popen(command, stdout=subprocess.PIPE, \
            stderr = subprocess.PIPE, universal_newlines=True, shell = True)
    start = time.time()
    error = process.stderr.readline()
    if error is not '':
        num_error += 1
        print('---------------------------Error--------------------------')
        print('File name: ' + file_name)
        print("Code: " + command)
        if type(error) is str:
            print(error, end='')
        else:
            print(error.decode(), end=b'')
        time_passed = 0
        while error and time_passed < 0.5:
            time_passed = time.time() -  start
            error = process.stderr.readline()
            if type(error) is str:
                print(error, end='')
            else:
                print(error.decode(), end=b'')
        flags = fcntl(process.stdout, F_GETFL)
        fcntl(process.stdout, F_SETFL, flags | O_NONBLOCK)
        for output in process.stdout:
            if type(output) is str:
                print(output, end='')
            else:
                print(output.decode(), end=b'')
        print('----------------------------------------------------------')
        print()
        exiting(True, skip_error, num_error)
        process.stderr.close()
        return num_error
    else:
        print("\nCode: " + command)
        for output in process.stdout:
            if type(output) is str:
                print(output, end='')
            else:
                print(output.decode(), end=b'')
        print()
    process.stdout.close()
    return num_error

def print_dict(map):
    """Loops through the map and just prints the steps
    """
    if type(map) is dict:
        for key in map:
            if is_command(key):
                print('|\t' + key[1:])
            else:
                print('|\t' + key)
                print_dict(map[key])
    elif type(map) is list:
        for line in map:
            print_dict(line)
    elif type(map) is str:
        if is_command(map):
            print('|\t' + map[1:])
        else:
            print('|\t' + map)
    else:
        print('Item: ' + string(map))
        raise TypeError("Item is not a dictionary, list, or string")

def loop_dict(num_error, map, run_all, skip_error, file_name, elbs_to_upgrade, \
                automated_elb, region):
    """Loops through the dictionary(created by opening the task yaml files)
    and returns the number of errors that occurred.

    @param num_error: The current number of errors throughout the script
    @param map: The dictionary
    @param run_all: (True) When we are running all the commands without
                    deciding on each step
    @param skip_error: (True) When we are going to skip all the errors that
                        occurred and continue running the script
    @param file_name: The name of the current file that we are reading the
                     commands from
    @param elbs_to_upgrade: The list of elbs that matches the load balancer name
                      that we wrote initially
    @param automated_elb: (True) When we run health check for every command
                          (False) When we need to decide for each step
    @param region: region for elb health check

    @return: total number of errors
    """
    if type(map) is dict:
        for key in map:
            if is_command(key):
                if automated_elb or run_check_healthy():
                    check_healthy(num_error, skip_error, elbs_to_upgrade, \
                                  region)
                num_error = run_command(map[key][1:], file_name, skip_error, \
                                        num_error)
            else:
                post_to_slack(key)
                print('\n'+key)
                if not run_all:
                    print_dict(map[key])
                if run_all or (not exiting(False, skip_error, num_error)):
                    num_error = loop_dict(num_error, map[key], run_all, skip_error, \
                                    file_name, elbs_to_upgrade, automated_elb, region)
    elif type(map) is list:
        for line in map:
            num_error = loop_dict(num_error, line, run_all, skip_error, file_name, \
                                    elbs_to_upgrade, automated_elb, region)
    elif type(map) is str:
        if is_command(map):
            if automated_elb or run_check_healthy():
                check_healthy(num_error, skip_error, elbs_to_upgrade, region)
            num_error = run_command(map[1:], file_name, skip_error, num_error)
        else:
            post_to_slack(map)
            print('\n' + map)
    else:
        print('Item: ' + string(map))
        raise TypeError("Item is not a dictionary, list, or string")

    return num_error

def post_to_slack(body):
    """posts the input string to slack if we didn't mute slack

    @param body: string to post to slack
    """
    if not MUTE_SLACK:
        webhook_url = 'https://hooks.slack.com/services/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' #'https://hooks.slack.com/services/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'
        slack_data = {'text': "" + body + ""}
        response = requests.post(
            webhook_url, data=json.dumps(slack_data),
            headers={'Content-Type': 'application/json'}
        )
        if response.status_code != 200:
            raise ValueError(
                'Request to slack returned an error %s, the response is:\n%s'
                % (response.status_code, response.text)
            )

def skip_error():
    """Determines if user wants to skip and continue the script without asking
    the user to continue or stop the script every time an error occurs

    @return True: skipping errors
    """
    skipping_error = input("Press [y] to skip errors or [n] to decide on each error: ")
    if skipping_error.lower().startswith('y'):
        return True
    elif skipping_error.lower().startswith('n'):
        return False
    else:
        return skip_error()

def run_all():
    """Determines if user wants to run all the commands without asking
    the user to continue or not for every command

    returns (skipping_error, running_all)
    @return skipping_error: (True) skip all errors and print them out later
    @return run_all: (True) run all the commands without deciding on each step
    """
    running_all = input("Press [y] to run everything or [n] to decide each step: ")
    if running_all.lower().startswith('y'):
        skipping_error = skip_error()
        return (skipping_error, True)
    elif running_all.lower().startswith('n'):
        return (False, False)
    else:
        return run_all()


def automatic_elb():
    """Determines whether to run health check for every command or to decide for
    each command that is ran

    @return True: run health check for every command without asking
    """
    automated_elb = input("Press [y] to run elb for every command or [n] to decide on each command: ")
    if automated_elb.lower().startswith('y'):
        return True
    elif automated_elb.lower().startswith('n'):
        return False
    else:
        return automatic_elb()

def run_check_healthy():
    """Determines whether to run health check for current command

    @return True: run health check for current command
    """
    run_check = input("Press [y] to run health check or [n] to not run health check: ")
    if run_check.lower().startswith('y'):
        return True
    elif run_check.lower().startswith('n'):
        return False
    else:
        return run_check_healthy()

def get_all_elbs(region):
    """Gets all the load balancer names from chosen region

    @param region: chosen region

    @return: list of all the load balancer names from chosen region
    """
    # elb_client = boto3.client('elb', region_name=region) # when running on salt master
    elb_client = boto3.Session(profile_name='saml').client('elb', region_name=region) # when running locally for debug
    elbs = elb_client.describe_load_balancers()
    all_elb_names = []
    for i in range (0, len(elbs['LoadBalancerDescriptions'])):
        elb_name = elbs['LoadBalancerDescriptions'][i]['LoadBalancerName']
        all_elb_names.append(elb_name)
    return all_elb_names

def get_elbs (elb_string_match, region):
    """Gets list of matching load balancer names from chosen name and region

    @param elb_string_match: string to match with load balancer names
    @param region: chosen region

    @return: list of matching load balancer names from chosen region
    """
    all_elb_names = get_all_elbs(region)
    matching_elbs = []
    for matching_elb in [m for m in all_elb_names if elb_string_match in m]:
        matching_elbs.append(matching_elb)
    return matching_elbs

def get_instance_health(elb_name, region):
    """Returns dictionary that classifies number of healthy/unhealthy/unknown
    instances

    @param elb_name: the name of the load balancer
    @param region: chosen region

    @return: dictionary with number of healthy/unhealthy/unknown instances
    """
    #elb_client = boto3.client('elb', region_name=region) # when running on salt master
    elb_client = boto3.Session(profile_name='saml').client('elb', region_name=region) # when running locally for debug
    elb_status = {}
    healthy_instance_count = 0
    unhealthy_instance_count = 0
    unknown_instance_count = 0
    elb_instance_health = elb_client.describe_instance_health(LoadBalancerName=elb_name)
    for i in range (0, len(elb_instance_health['InstanceStates'])):
        instance_health = elb_instance_health['InstanceStates'][i]['State']
        if 'InService' in instance_health:
            healthy_instance_count += 1
        elif 'OutOfService' in instance_health:
            unhealthy_instance_count += 1
        else:
            unknown_instance_count += 1
    elb_status.update({"name":elb_name})
    elb_status.update({"healthy":healthy_instance_count})
    elb_status.update({"unhealthy":unhealthy_instance_count})
    elb_status.update({"unknown":unknown_instance_count})
    return elb_status

def wait_for_healthy(region, elb_name, skip_error, num_error):
    """Waits until the number healthy instances is greater or equal to the
    desired number of healthy instances. When we waited for more than 4 times,
    prompts user to decide whether to continue waiting or to return to the script

    @param region: chosen region
    @param elb_name: the name of the load balancer
    @param skip_error: needed to call exiting(False, skip_error, num_error)
    @param num_error: needed to call exiting(False, skip_error, num_error)

    @return True: if successful to find desired number of healthy instances.
    @return False: if unsuccessful to find desired number of healthy instances.
    """
    break_count = 0
    healthy_counter = get_instance_health(elb_name, region)['healthy']
    print("ELB: " + str(elb_name) + " - Healthy Instance Count: " + str(healthy_counter) + " Desired Healthy Instance Count: " + str(Desired_health_count))
    while healthy_counter < Desired_health_count:
####################################################################################################
        # Change the break_count to higher before going live <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
        if break_count > 4:
            print("Pausing script for manual intervention of healthy instance count in ELB: " + str(elb_name))
            exit = exiting(False, skip_error, num_error)
            # exiting() returns False: continue running
            # exiting() returns True: skip to next step

            if exit:
                return False #Fail to get more than count number of healthy instances
            else:
                break_count = 0
        else:
            print("ELB: " + str(elb_name) + " - Healthy Instance Count: " + str(healthy_counter) + " Desired Healthy Instance Count: " + str(Desired_health_count))
            break_count += 1
            time.sleep(15)
    return True

def check_healthy(num_error, skip_error, elbs_to_upgrade, region):
    """Loops through the list of matching elbs and prints out the result of
    running wait_for_healthy().

    @param num_error: needed to call wait_for_healthy()
    @param skip_error: needed to call wait_for_healthy()
    @param elbs_to_upgrade: list of matching elbs
    @param region: chosen region
    """
    print("Checking ELBs for minimum healthy instance count: " + str(Desired_health_count))
    post_to_slack("Checking ELBs for minimum healthy instance count: " + str(Desired_health_count))
    if len(elbs_to_upgrade) == 0:
        print("No matching elbs")
    for i in range (0, len(elbs_to_upgrade)):
        elb_name = elbs_to_upgrade[i]
        success = wait_for_healthy(region, elb_name, skip_error, num_error)
        if success:
            print("ELB: " + str(elb_name) + " - Succeeded finding minimum healthy instance count of " + str(Desired_health_count))
        else:
            print("ELB: " + str(elb_name) + " - Failed to find minimum healthy instance count of " + str(Desired_health_count))
            break
    print()

def main(file_names):

    # direc = input('Enter your region: ')
    # (Use above direc function if want to use different directory every time)
    direc = 'task/'

    for count, file_name in enumerate(file_names):
        try:
            directory = direc + file_name
            with open(directory) as f:
                num_error = 0

                automated_elb = automatic_elb()
                # Obtaining load balancer name and region to use
                elb_string_match = input('Enter load balancer name: ')
                region = input('Enter your region: ') #Can change to ur region if it is the same every time
                elbs_to_upgrade = get_elbs(elb_string_match, region)
                if len(elbs_to_upgrade) == 0 and elb_string_match != 'dontrun':
                    # If there is no matching load balancer names, gives a
                    # list of load balancer names in the region and prompt
                    # user to try again
                    print("\nNo matching elbs. All elbs in \'" + region + '\' are:' )
                    all_elbs = get_all_elbs(region)
                    print(all_elbs)
                    print('\nTry again. Type "dontrun" if you don\'t want to run health check')
                    elb_string_match = input('Enter load balancer name: ')
                    elbs_to_upgrade = get_elbs(elb_string_match, region)

                # Open file, create a dictionary, and loop through instructions
                print()
                print('------------------------------------------------------------')
                print('Opened ' + file_name)
                (skipping_error, running_all) = run_all()
                datamap = yaml.safe_load(f)
                num_error = loop_dict(num_error, datamap, running_all, skipping_error, \
                                    file_name, elbs_to_upgrade, automated_elb, region)
                print('\nFinished Running')
                post_to_slack('Finished Running')
                print('Total ' + str(num_error) + ' errors')
                post_to_slack('Total '+ str(num_error) + ' errors')
                if not num_error:
                    print('\nSuccess!')
                    post_to_slack('Success!')

        # If the file is not found, gives the user a list of file names in the
        # directory and prompts to try again
        except FileNotFoundError:
            print()
            print('\'' + file_name + '\'' + ' file not found. Try again.')
            print('Files in \'' + direc + '\' directory:')
            files = [f for f in os.listdir(direc) \
                    if os.path.isfile(os.path.join(direc, f))]
            for f in files:
                print(f)
            print()
            new_name =  input('Enter your file name: ')
            file_names[count] = new_name
            main(file_names)

if __name__ == '__main__':
    main(sys.argv[1:-1])
