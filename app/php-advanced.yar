import "hash"
include "whitelist.yar"

/* Modern obfuscation techniques for PHP webshells */

rule ModernPhpObfuscation
{
    strings:
        $create_function = /create_function\s*\(\s*['"][^'"]*['"],\s*['"]/ nocase
        $namespaces = /namespace\s+\w+;.*\$_(GET|POST|REQUEST|COOKIE)/ nocase
        $goto = /goto\s+\w+;.*:\s*/ nocase
        $unicode = /\\u[0-9a-f]{4}/ nocase
        $octal = /\\[0-7]{3}/ nocase
        $compact = /compact\s*\(\s*['"]\$/ nocase
        $variable_unserialize = /unserialize\s*\(\s*\$_(GET|POST|REQUEST|COOKIE)/ nocase
        $reflection = /ReflectionClass|ReflectionMethod|ReflectionFunction/ nocase
        $call_user_func_array = /call_user_func_array\s*\(\s*\$_(GET|POST|REQUEST|COOKIE)/ nocase

    condition:
        2 of them and not IsWhitelisted
}
