"""The workflow-expression checker (#81).

`tools/check_workflows.py` scans text rather than parsing YAML, because
`tests/unit` runs on `requests` alone and adding PyYAML to that lane to
read three files would cost more than the scanning is worth.

That trade is only sound if the shapes it could get wrong are pinned
down, so this file is mostly those: block scalars in each of their
spellings, an expression in a YAML field that must **not** be reported,
and the boundary where a `run:` block ends.

The false-positive cases matter more than the true ones. A checker that
flags `with:` arguments would be turned off within a week, and then the
thing it was guarding goes unguarded.
"""

import importlib.util
import pathlib
import tempfile
import unittest


def _load():
    path = (pathlib.Path(__file__).resolve().parents[2]
            / "tools" / "check_workflows.py")
    spec = importlib.util.spec_from_file_location("check_workflows", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load()


def lines_of(text):
    return [number for number, _line in checker.offending_lines(text)]


class TestInterpolationIsFound(unittest.TestCase):
    def test_a_literal_block_body(self):
        text = (
            "    steps:\n"
            "      - name: x\n"
            "        run: |\n"
            "          v='${{ steps.a.outputs.b }}'\n"
        )

        self.assertEqual(lines_of(text), [4])

    def test_a_run_written_on_one_line(self):
        # Just as interpolated as a block, and easy to miss by looking
        # only for `run: |`.
        text = '      - run: echo "${{ github.ref }}"\n'

        self.assertEqual(lines_of(text), [1])

    def test_a_folded_block(self):
        text = (
            "        run: >\n"
            "          echo ${{ github.ref }}\n"
        )

        self.assertEqual(lines_of(text), [2])

    def test_block_indicators_do_not_hide_it(self):
        # `|-`, `|+`, `>-` and an explicit indentation indicator are all
        # the same block scalar with the same exposure.
        for indicator in ('|', '|-', '|+', '>', '>-', '>+', '|2'):
            with self.subTest(indicator=indicator):
                text = ("        run: %s\n"
                        "          echo ${{ github.ref }}\n" % indicator)

                self.assertEqual(lines_of(text), [2])

    def test_every_offending_line_is_reported_not_just_the_first(self):
        text = (
            "        run: |\n"
            "          a='${{ steps.a.outputs.x }}'\n"
            "          echo fine\n"
            "          b='${{ steps.a.outputs.y }}'\n"
        )

        self.assertEqual(lines_of(text), [2, 4])

    def test_a_blank_line_does_not_end_the_block(self):
        text = (
            "        run: |\n"
            "          echo one\n"
            "\n"
            "          echo '${{ github.ref }}'\n"
        )

        self.assertEqual(lines_of(text), [4])

    def test_the_list_item_form_is_recognised(self):
        text = (
            "      - run: |\n"
            "          echo '${{ github.ref }}'\n"
        )

        self.assertEqual(lines_of(text), [2])


class TestShapesThatCouldHideAScript(unittest.TestCase):
    """False negatives. The failure mode that matters most.

    A false positive is noisy and gets noticed. A block header this
    scanner does not recognise is read as an ordinary scalar, so the
    script under it is never scanned -- and the checker reports success
    over exactly the thing it exists to find.
    """

    def test_a_comment_after_the_block_indicator(self):
        # `run: | # build it` is a block. Requiring end-of-line after
        # the `|` makes it look like a plain scalar instead, and the
        # interpolation two lines down goes unseen.
        text = (
            "        run: | # build it\n"
            "          echo start\n"
            "          v='${{ steps.a.outputs.b }}'\n"
        )

        self.assertEqual(lines_of(text), [3])

    def test_a_comment_after_a_folded_indicator_with_chomping(self):
        text = (
            "        run: >- # folded\n"
            "          echo '${{ github.ref }}'\n"
        )

        self.assertEqual(lines_of(text), [2])

    def test_the_two_patterns_agree_about_what_a_block_is(self):
        """BLOCK and INLINE must partition, not overlap or leave a gap.

        A header both reject is the dangerous case: no block opens, and
        nothing scans what follows.
        """
        for header in ('run: |', 'run: |-', 'run: > # note',
                       'run: |2', 'run: | # x', 'run: >+'):
            with self.subTest(header=header):
                line = '        %s' % header

                self.assertIsNotNone(
                    checker.BLOCK.match(line),
                    'no block opens, so its script is never scanned')
                self.assertIsNone(
                    checker.INLINE.match(line),
                    'read as an inline script as well as a block')


class TestYamlFieldsAreNotFlagged(unittest.TestCase):
    """Expressions belong in YAML fields. Only scripts are the problem."""

    def test_an_expression_in_with_is_fine(self):
        text = (
            "      - uses: actions/checkout@v7\n"
            "        with:\n"
            "          ref: ${{ github.event.inputs.tag || github.ref }}\n"
        )

        self.assertEqual(lines_of(text), [])

    def test_an_expression_in_env_is_the_recommended_fix(self):
        # Reporting this would flag the remedy as the defect.
        text = (
            "        env:\n"
            "          VERSION: ${{ steps.version.outputs.version }}\n"
            "        run: |\n"
            "          echo \"${VERSION}\"\n"
        )

        self.assertEqual(lines_of(text), [])

    def test_an_expression_in_a_step_name_is_fine(self):
        text = "      - name: Log in to ${{ env.REGISTRY }}\n"

        self.assertEqual(lines_of(text), [])

    def test_the_block_ends_at_the_next_key(self):
        """The boundary this scanner most plausibly gets wrong.

        A `with:` following a `run:` block is dedented back to the step's
        own level. Treating it as still inside the script would report
        every expression after any `run:` in the file.
        """
        text = (
            "      - name: one\n"
            "        run: |\n"
            "          echo safe\n"
            "      - name: two\n"
            "        uses: docker/build-push-action@v6\n"
            "        with:\n"
            "          tags: ${{ steps.version.outputs.image }}\n"
        )

        self.assertEqual(lines_of(text), [])

    def test_a_compact_list_item_step_keeps_its_own_keys_out(self):
        """The step's `env:` sits *inside* the dash's indentation.

        `- run: |` puts `run:` two columns right of the dash, and the
        step's sibling keys line up with `run:`, not with the dash. A
        floor taken from the leading whitespace is therefore two columns
        too shallow, and every key after the block reads as script --
        reporting the recommended fix as the defect, which is the way
        this checker gets switched off.
        """
        text = (
            "      - run: |\n"
            "          echo hello\n"
            "        env:\n"
            "          FOO: ${{ steps.a.outputs.b }}\n"
        )

        self.assertEqual(lines_of(text), [])

    def test_a_shell_variable_is_not_an_expression(self):
        # `${VERSION}` is what the fix looks like; only `${{` is GitHub's.
        text = (
            "        run: |\n"
            "          echo \"${VERSION}\" \"$GITHUB_REF_NAME\"\n"
        )

        self.assertEqual(lines_of(text), [])


class TestAgainstTheRealWorkflows(unittest.TestCase):
    def test_this_repository_is_clean(self):
        """The rule, enforced. This is the test that fails on a new one.

        Scoped to the real directory rather than a fixture, so adding an
        interpolation to any workflow fails here without anyone having
        to remember this file exists.
        """
        workflows = (pathlib.Path(__file__).resolve().parents[2]
                     / ".github" / "workflows")

        problems = checker.check(workflows)

        self.assertEqual(
            [], problems,
            "pass these through env: instead:\n%s"
            % "\n".join("  %s:%d %s" % (p.name, n, line)
                        for p, n, line in problems))

    def test_check_finds_an_offender_in_a_directory(self):
        """A clean result and an empty search look identical.

        The test above passes whether `check()` reads the workflows or
        reads nothing at all, so this drives the same entry point over a
        directory that is known to contain one offender. Globbing the
        directory here instead would prove only that this test can list
        files, which is not the thing in doubt.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "clean.yml").write_text(
                "        env:\n"
                "          V: ${{ steps.a.outputs.b }}\n"
                "        run: |\n"
                "          echo \"${V}\"\n")
            (root / "dirty.yml").write_text(
                "        run: |\n"
                "          echo '${{ steps.a.outputs.b }}'\n")

            problems = checker.check(root)

        self.assertEqual([(p.name, n) for p, n, _line in problems],
                         [("dirty.yml", 2)])

    def test_the_real_workflow_directory_is_where_it_is_expected(self):
        # If this repo is ever restructured, the guard above would start
        # scanning nothing and keep passing.
        workflows = (pathlib.Path(__file__).resolve().parents[2]
                     / ".github" / "workflows")

        found = sorted(path.name for path in workflows.glob("*.y*ml"))

        self.assertIn("image.yml", found)
        self.assertGreaterEqual(len(found), 3)


if __name__ == "__main__":
    unittest.main()
