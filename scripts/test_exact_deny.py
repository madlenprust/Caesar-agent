"""Тест exact_deny — зашитый чёрный список необратимых операций.

Проверяет, что обходы (chaining &&;||;|, $() ``, eval, reorder флагов
rm -fr / rm -r -f) ловятся, а безопасные команды проходят.
PRINCIPLES #9: exact_deny не отключается через UI/чат.
"""
import pytest

from caesar.tools.base import is_dangerous_command

# Команды, которые ДОЛЖНЫ блокироваться (в т.ч. известные обходы).
DENY = [
    pytest.param("rm -rf /", id="rm-rf-root"),
    pytest.param("rm -rf /etc", id="rm-rf-etc"),
    pytest.param("rm -rf /home", id="rm-rf-home"),
    pytest.param("rm -rf /usr", id="rm-rf-usr"),
    pytest.param("rm -rf /var", id="rm-rf-var"),
    pytest.param("rm -rf ~", id="rm-rf-home-tilde"),
    pytest.param("rm -fr /", id="rm-fr-reorder"),          # обход: порядок флагов
    pytest.param("rm -r -f /etc", id="rm-r-f-split"),      # обход: флаги отдельно
    pytest.param("rm -rf /usr/local", id="rm-rf-usr-subdir"),
    pytest.param("cd /tmp && rm -rf /", id="chain-amp"),   # обход: &&
    pytest.param("echo x; rm -rf /etc", id="chain-semicolon"),  # обход: ;
    pytest.param("true | rm -rf /home", id="chain-pipe"),  # обход: |
    pytest.param("$(rm -rf /home)", id="cmd-subst"),       # обход: $(...)
    pytest.param("`rm -rf /`", id="backticks"),            # обход: `...`
    pytest.param('eval "rm -rf /var"', id="eval"),         # обход: eval
    pytest.param("mkfs.ext4 /dev/sda", id="mkfs-dev"),
    pytest.param("dd if=/dev/zero of=/dev/sda", id="dd-to-dev"),
    pytest.param("chmod -R 777 /", id="chmod-777-root"),
    pytest.param("chmod -R 000 /", id="chmod-000-root"),
    pytest.param("chown -R user /", id="chown-root"),
    pytest.param(":(){ :|:& };:", id="fork-bomb"),
    pytest.param('bash -c "rm -rf /home"', id="bash-c-rm"),
    pytest.param('sh -c "rm -rf /etc"', id="sh-c-rm"),
    pytest.param('python3 -c "import os; os.system(\'rm -rf /\')"', id="py-c-os-system"),
    pytest.param("pip uninstall -y pip", id="uninstall-pip"),
    pytest.param("> /dev/sda", id="redirect-to-block-dev"),
    pytest.param("systemctl disable sshd", id="disable-sshd"),
]

# Команды, которые НЕ должны блокироваться exact_deny (безопасные или /tmp).
ALLOW = [
    pytest.param("ls -la /", id="ls"),
    pytest.param("cat /etc/hostname", id="cat-etc-hostname"),
    pytest.param("rm -rf /tmp/foo", id="rm-rf-tmp"),
    pytest.param("rm -rf /var/tmp/x", id="rm-rf-var-tmp"),
    pytest.param("rm -rf ./build", id="rm-rf-rel-build"),
    pytest.param("rm file.txt", id="rm-single-no-rf"),
    pytest.param("chmod 644 file", id="chmod-no-R"),
    pytest.param("chmod -R 755 dir", id="chmod-R-755"),
    pytest.param("mkfs.ext4 /mnt/usb", id="mkfs-mnt"),
    pytest.param("pip install requests", id="pip-install"),
    pytest.param("sudo apt update", id="sudo-apt-update"),
    pytest.param("git pull", id="git-pull"),
    pytest.param("curl https://example.com", id="curl"),
]


@pytest.mark.parametrize("cmd", DENY)
def test_exact_deny_blocks(cmd):
    is_dangerous, reason = is_dangerous_command(cmd)
    assert is_dangerous, f"должно блокироваться, но прошло: {cmd!r}"


@pytest.mark.parametrize("cmd", ALLOW)
def test_exact_deny_allows(cmd):
    is_dangerous, reason = is_dangerous_command(cmd)
    assert not is_dangerous, f"не должно блокироваться, но залочилось: {cmd!r} (reason={reason})"
