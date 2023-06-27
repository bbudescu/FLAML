from .agent import Agent
from flaml.autogen.code_utils import UNKNOWN, extract_code, execute_code, infer_lang
from collections import defaultdict
import json
from typing import Dict, Union


class UserProxyAgent(Agent):
    """(Experimental) A proxy agent for the user, that can execute code and provide feedback to the other agents."""

    MAX_CONSECUTIVE_AUTO_REPLY = 100  # maximum number of consecutive auto replies (subject to future change)

    def __init__(
        self,
        name,
        system_message="",
        work_dir=None,
        human_input_mode="ALWAYS",
        function_map=defaultdict(dict),
        max_consecutive_auto_reply=None,
        is_termination_msg=None,
        use_docker=True,
        **config,
    ):
        """
        Args:
            name (str): name of the agent
            system_message (str): system message to be sent to the agent
            work_dir (str): working directory for the agent
            human_input_mode (str): whether to ask for human inputs every time a message is received.
                Possible values are "ALWAYS", "TERMINATE", "NEVER".
                (1) When "ALWAYS", the agent prompts for human input every time a message is received.
                    Under this mode, the conversation stops when the human input is "exit",
                    or when is_termination_msg is True and there is no human input.
                (2) When "TERMINATE", the agent only prompts for human input only when a termination message is received or
                    the number of auto reply reaches the max_consecutive_auto_reply.
                (3) When "NEVER", the agent will never prompt for human input. Under this mode, the conversation stops
                    when the number of auto reply reaches the max_consecutive_auto_reply or when is_termination_msg is True.
            function_map (dict[str, dict]): a dictionary of dictionaries.
                the outer dictionary maps function names (passed to openai) to two types of functions:
                    (1) A function to be called directly: {
                        "function" (Required, callable): a callable function that will be called
                    }
                    (2) A function in a class to be called: {
                        "class" (Required): an instance of a class.
                        "func_name" (Optional, str): name of the function in the class. If not given the class will be called directly.
                    }

                Example 1: 
                def add_num_func(num_to_be_added):
                    given_num = 10
                    return num_to_be_added + given_num
                oai_config = {"functions": [{"name": "add_num",...}]} # oai config, this will be passed to AssistantAgent
                user = UserProxyAgent(name="test", function_map={"add_num": {"function": add_num_func}}) 

                func_call = {"name": "add_num", "args": {"num_to_be_added": 5}} # this is a function call passed from the LLM assistant
                user._execute_function(func_call) # this will call add_num_func(5) and return 15

                Example 2:
                oai_config = {"functions": [{"name": "add_num",...}]} # oai config, this will be passed to AssistantAgent

                class AddNum:
                    def __init__(self, given_num):
                        self.given_num = given_num

                    def add(self, num_to_be_added):
                        self.given_num = num_to_be_added + self.given_num
                        return self.given_num

                            user = UserProxyAgent(
                                name="test",
                                function_map={
                                    "add_num": {
                                        "class": AddNum(given_num=10),
                                        "func_name": "add",
                                    }
                                },
                            )
                func_call = {"name": "add_num", "arguments": '{ "num_to_be_added": 5 }'} # assume this is a function call passed from the LLM assistant
                user._execute_function(func_call) # this will call AddNum.add_num_func(5) and return 15
                user._execute_function(func_call) # this will call AddNum.add_num_func(5) and return 20. The same class instance is used.
            max_consecutive_auto_reply (int): the maximum number of consecutive auto replies.
                default to None (no limit provided, class attribute MAX_CONSECUTIVE_AUTO_REPLY will be used as the limit in this case).
                The limit only plays a role when human_input_mode is not "ALWAYS".
            is_termination_msg (function): a function that takes a dictionary (a message) and determine if this received message is a termination message.
                The dict can contain the following keys: "content", "role", "name", "function_call".
            use_docker (bool): whether to use docker to execute the code.
            **config (dict): other configurations.
        """
        super().__init__(name, system_message)
        self._work_dir = work_dir
        self._human_input_mode = human_input_mode
        self._is_termination_msg = (
            is_termination_msg
            if is_termination_msg is not None
            else (lambda x: x == "TERMINATE" if isinstance(x, str) else x.get("content") == "TERMINATE")
        )
        self._config = config
        self._max_consecutive_auto_reply = (
            max_consecutive_auto_reply if max_consecutive_auto_reply is not None else self.MAX_CONSECUTIVE_AUTO_REPLY
        )
        self._consecutive_auto_reply_counter = defaultdict(int)
        self._use_docker = use_docker

        # 'class' and 'function' cannot exist at the same time.
        for f in function_map:
            assert ("class" in function_map[f] or "function" in function_map[f]) and ("class" in function_map[f]) != (
                "function" in function_map[f]
            ), "only one of 'class' and 'function' can exist in a function config."
        self._function_map = function_map

    def _execute_code(self, code_blocks):
        """Execute the code and return the result."""
        logs_all = ""
        for code_block in code_blocks:
            lang, code = code_block
            if not lang:
                lang = infer_lang(code)
            if lang in ["bash", "shell", "sh"]:
                # if code.startswith("python "):
                #     # return 1, f"please do not suggest bash or shell commands like {code}"
                #     file_name = code[len("python ") :]
                #     exitcode, logs = execute_code(filename=file_name, work_dir=self._work_dir, use_docker=self._use_docker)
                # else:
                exitcode, logs, image = execute_code(
                    code, work_dir=self._work_dir, use_docker=self._use_docker, lang=lang
                )
                logs = logs.decode("utf-8")
            elif lang == "python":
                if code.startswith("# filename: "):
                    filename = code[11 : code.find("\n")].strip()
                else:
                    filename = None
                exitcode, logs, image = execute_code(
                    code, work_dir=self._work_dir, filename=filename, use_docker=self._use_docker
                )
                logs = logs.decode("utf-8")
            else:
                # TODO: could this happen?
                exitcode, logs, image = 1, f"unknown language {lang}"
                # raise NotImplementedError
            self._use_docker = image
            logs_all += "\n" + logs
            if exitcode != 0:
                return exitcode, logs_all
        return exitcode, logs_all

    @staticmethod
    def _format_json_str(jstr):
        """Remove newlines outside of quotes, and hanlde JSON escape sequences.

        1. this function removes the newline in the query outside of quotes otherwise json.loads(s) will fail.
            Ex 1:
            "{\n"tool": "python",\n"query": "print('hello')\nprint('world')"\n}" -> "{"tool": "python","query": "print('hello')\nprint('world')"}"
            Ex 2:
            "{\n  \"location\": \"Boston, MA\"\n}" -> "{"location": "Boston, MA"}"

        2. this function also handles JSON escape sequences inside quotes,
            Ex 1:
            '{"args": "a\na\na\ta"}' -> '{"args": "a\\na\\na\\ta"}'
        """
        result = []
        inside_quotes = False
        last_char = ' '
        for char in jstr:
            if last_char != '\\' and char == '"':
                inside_quotes = not inside_quotes
            last_char = char
            if not inside_quotes and char == "\n":
                continue
            if inside_quotes and char == "\n":
                char = "\\n"
            if inside_quotes and char == "\t":
                char = "\\t"
            result.append(char)
        return "".join(result)

    def _extract_args(self, input_string: str):
        """Extract arguments from a json-like string and put it into a dict.

        Args:
            input_string: string to extract arguments from.

        Returns:
            a dictionary or None.
        """
        input_string = self._format_json_str(input_string)
        try:
            args = json.loads(input_string)
            return args
        except json.JSONDecodeError:
            return {}

    def _execute_function(self, func_call):
        """Execute a function call and return the result.

        Args:
            func_call: a dictionary extracted from openai message at key "function_call" with keys "name" and "arguments".

        Returns:
            A tuple of (is_exec_success, result_dict).
            is_exec_success (boolean): whether the execution is successful.
            result_dict: a dictionary with keys "name", "role", and "content". Value of "role" is "function".
        """
        func_name = func_call.get("name", "")
        func_dict = self._function_map.get(func_name, None)

        is_exec_success = False
        if func_dict is not None:
            arguments = self._extract_args(func_call.get("arguments", ""))
            arguments.update(func_dict.get("args", {}))

            try:
                func = (
                    getattr(func_dict["class"], func_dict.get("func_name", None), func_dict["class"])
                    if "class" in func_dict
                    else func_dict["function"]
                )
                content = func(**arguments)
                is_exec_success = True
            except Exception as e:
                content = f"Error: {e}"

        else:
            content = f"Error: Function {func_name} not found."

        return is_exec_success, {
            "name": func_name,
            "role": "function",
            "content": str(content),
        }

    def auto_reply(self, message: dict, sender, default_reply=""):
        """Generate an auto reply."""
        if "function_call" in message:
            is_exec_success, func_return = self._execute_function(message["function_call"])
            self._send(func_return, sender)
            return

        code_blocks = extract_code(message["content"])
        if len(code_blocks) == 1 and code_blocks[0][0] == UNKNOWN:
            # no code block is found, lang should be `UNKNOWN`
            self._send(default_reply, sender)
        else:
            # try to execute the code
            exitcode, logs = self._execute_code(code_blocks)
            exitcode2str = "execution succeeded" if exitcode == 0 else "execution failed"
            self._send(f"exitcode: {exitcode} ({exitcode2str})\nCode output: {logs}", sender)

    def receive(self, message: Union[Dict, str], sender):
        """Receive a message from the sender agent.
        Once a message is received, this function sends a reply to the sender or simply stop.
        The reply can be generated automatically or entered manually by a human.
        """
        if type(message) is str:
            message = {"content": message, "role": "user"}
        super().receive(message, sender)
        # default reply is empty (i.e., no reply, in this case we will try to generate auto reply)
        reply = ""
        if self._human_input_mode == "ALWAYS":
            reply = input(
                "Provide feedback to the sender. Press enter to skip and use auto-reply, or type 'exit' to end the conversation: "
            )
        elif self._consecutive_auto_reply_counter[
            sender.name
        ] >= self._max_consecutive_auto_reply or self._is_termination_msg(message):
            if self._human_input_mode == "TERMINATE":
                reply = input(
                    "Please give feedback to the sender. (Press enter or type 'exit' to stop the conversation): "
                )
                reply = reply if reply else "exit"
            else:
                # this corresponds to the case when self._human_input_mode == "NEVER"
                reply = "exit"
        if reply == "exit" or (self._is_termination_msg(message) and not reply):
            # reset the consecutive_auto_reply_counter
            self._consecutive_auto_reply_counter[sender.name] = 0
            return
        if reply:
            # reset the consecutive_auto_reply_counter
            self._consecutive_auto_reply_counter[sender.name] = 0
            self._send(reply, sender)
            return

        self._consecutive_auto_reply_counter[sender.name] += 1
        print("\n>>>>>>>> NO HUMAN INPUT RECEIVED. USING AUTO REPLY FOR THE USER...", flush=True)
        self.auto_reply(message, sender, default_reply=reply)
