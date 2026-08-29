"""Entry point for the WP4 parity corpus."""

from calculator import Calculator


def main():
    calc = Calculator()
    return calc.total(2, 3)
