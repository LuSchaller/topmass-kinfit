# coding: utf-8

"""
Tasks to create trigger correction weights .
"""

from collections import OrderedDict, defaultdict
from abc import abstractmethod

from columnflow.types import Any

import law
import order as od

from columnflow.tasks.framework.base import Requirements, ShiftTask
from columnflow.tasks.framework.mixins import (
    CalibratorClassesMixin, SelectorClassMixin, ReducerClassMixin, ProducerClassesMixin, HistProducerClassMixin,
    CategoriesMixin, HistHookMixin, MLModelsMixin,
)
from columnflow.tasks.framework.plotting import (
    PlotBase, PlotBase1D, ProcessPlotSettingMixin, VariablePlotSettingMixin,
)
from columnflow.tasks.framework.decorators import view_output_plots
from columnflow.tasks.framework.remote import RemoteWorkflow
from columnflow.tasks.histograms import MergeHistograms
from columnflow.util import DotDict, dev_sandbox
from columnflow.hist_util import add_missing_shifts
from columnflow.config_util import get_shift_from_configs


class _ProduceTriggerWeightBase(
    CalibratorClassesMixin,
    SelectorClassMixin,
    ReducerClassMixin,
    ProducerClassesMixin,
    MLModelsMixin,
    HistProducerClassMixin,
    CategoriesMixin,
    ProcessPlotSettingMixin,
    VariablePlotSettingMixin,
    HistHookMixin,
    law.LocalWorkflow,
    RemoteWorkflow,
):
    """
    Base classes for :py:class:`PlotVariablesBase`.
    """


class ProduceTriggerWeightBase(_ProduceTriggerWeightBase):
    single_config = False

    sandbox = dev_sandbox(law.config.get("analysis", "default_columnar_sandbox"))

    exclude_index = True

    def store_parts(self) -> law.util.InsertableDict:
        parts = super().store_parts()
        parts.insert_before("version", "datasets", f"datasets_{self.datasets_repr}")
        return parts

    def create_branch_map(self):
        return [
            DotDict({"category": cat_name, "variable": var_name})
            for cat_name in sorted(self.categories)
            for var_name in sorted(self.variables)
        ]

    def workflow_requires(self):
        reqs = super().workflow_requires()
        reqs["merged_hists"] = self.requires_from_branch()
        return reqs

    @abstractmethod
    def get_plot_shifts(self):
        return

    @property
    def config_inst(self):
        return self.config_insts[0]

    def get_config_process_map(self) -> tuple[dict[od.Config, dict[od.Process, dict[str, Any]]], dict[str, set[str]]]:
        """
        Function that maps the config and process instances to the datasets and shifts they are supposed to be plotted
        with. The mapping from processes to datasets is done by checking the dataset instances for the presence of the
        process instances. The mapping from processes to shifts is done by checking the upstream requirements for the
        presence of a shift in the requires method of the task.

        :return: A 2-tuple with a dictionary mapping config instances to dictionaries mapping process instances to
            dictionaries containing the dataset-process mapping and the shifts to be considered, and a dictionary
            mapping process names to the shifts to be considered.
        """
        reqs = self.requires()

        config_process_map = {config_inst: {} for config_inst in self.config_insts}
        process_shift_map = defaultdict(set)

        for i, config_inst in enumerate(self.config_insts):
            process_insts = [config_inst.get_process(p) for p in self.processes[i]]
            dataset_insts = [config_inst.get_dataset(d) for d in self.datasets[i]]

            requested_shifts_per_dataset: dict[od.Dataset, list[od.Shift]] = {}
            for dataset_inst in dataset_insts:
                _req = reqs[config_inst.name][dataset_inst.name]
                if hasattr(_req, "shift") and _req.shift:
                    # when a shift is found, use it
                    requested_shifts = [_req.shift]
                else:
                    # when no shift is found, check upstream requirements
                    requested_shifts = [sub_req.shift for sub_req in _req.requires().values()]

                requested_shifts_per_dataset[dataset_inst] = requested_shifts

            for process_inst in process_insts:
                sub_process_insts = [sub for sub, _, _ in process_inst.walk_processes(include_self=True)]
                dataset_proc_name_map = {}
                for dataset_inst in dataset_insts:
                    matched_proc_names = [p.name for p in sub_process_insts if dataset_inst.has_process(p.name)]
                    if matched_proc_names:
                        dataset_proc_name_map[dataset_inst] = matched_proc_names

                if not dataset_proc_name_map:
                    # no datasets found for this process
                    continue

                process_info = {
                    "dataset_proc_name_map": dataset_proc_name_map,
                    "config_shifts": {
                        shift
                        for dataset_inst in dataset_proc_name_map.keys()
                        for shift in requested_shifts_per_dataset[dataset_inst]
                    },
                }
                process_shift_map[process_inst.name].update(process_info["config_shifts"])
                config_process_map[config_inst][process_inst] = process_info

        # assign the combination of all shifts to each config-process pair
        for config_inst, process_info_dict in config_process_map.items():
            for process_inst, process_info in process_info_dict.items():
                if process_inst.name in process_shift_map:
                    config_process_map[config_inst][process_inst]["shifts"] = process_shift_map[process_inst.name]

        return config_process_map, process_shift_map

    @law.decorator.log
    @view_output_plots
    def run(self):
        import hist

        # prepare other config objects
        variable_tuple = self.variable_tuples[self.branch_data.variable]
        variable_insts = [
            self.config_inst.get_variable(var_name)
            for var_name in variable_tuple
        ]
        plot_shifts = self.get_plot_shifts()
        plot_shift_names = set(shift_inst.name for shift_inst in plot_shifts)

        # get assignment of processes to datasets and shifts
        config_process_map, process_shift_map = self.get_config_process_map()

        # histogram data per process copy
        hists: dict[od.Config, dict[od.Process, hist.Hist]] = {}
        with self.publish_step(f"plotting {self.branch_data.variable} in {self.branch_data.category}"):
            for i, (config, dataset_dict) in enumerate(self.input().items()):
                config_inst = self.config_insts[i]
                category_inst = config_inst.get_category(self.branch_data.category)
                leaf_category_insts = category_inst.get_leaf_categories() or [category_inst]

                hists_config = {}

                for dataset, inp in dataset_dict.items():
                    dataset_inst = config_inst.get_dataset(dataset)
                    h_in = inp["collection"][0]["hists"].targets[self.branch_data.variable].load(formatter="pickle")

                    # loop and extract one histogram per process
                    for process_inst, process_info in config_process_map[config_inst].items():
                        if dataset_inst not in process_info["dataset_proc_name_map"].keys():
                            continue

                        # select processes and reduce axis
                        h = h_in.copy()
                        h = h[{
                            "process": [
                                hist.loc(proc_name)
                                for proc_name in process_info["dataset_proc_name_map"][dataset_inst]
                                if proc_name in h.axes["process"]
                            ],
                        }]
                        h = h[{"process": sum}]

                        # create expected shift bins and fill them with the nominal histogram
                        expected_shifts = plot_shift_names & process_shift_map[process_inst.name]
                        add_missing_shifts(h, expected_shifts, str_axis="shift", nominal_bin="nominal")

                        # add the histogram
                        if process_inst in hists_config:
                            hists_config[process_inst] += h
                        else:
                            hists_config[process_inst] = h

                # after merging all processes, sort the histograms by process order and store them
                hists[config_inst] = {
                    proc_inst: hists_config[proc_inst]
                    for proc_inst in sorted(
                        hists_config.keys(), key=list(config_process_map[config_inst].keys()).index,
                    )
                }

                # there should be hists to plot
                if not hists:
                    raise Exception(
                        "no histograms found to plot; possible reasons:\n"
                        "  - requested variable requires columns that were missing during histogramming\n"
                        "  - selected --processes did not match any value on the process axis of the input histogram",
                    )

            # update histograms using custom hooks
            hists = self.invoke_hist_hooks(hists)

            # merge configs
            if len(self.config_insts) != 1:
                process_memory = {}
                merged_hists = {}
                for _hists in hists.values():
                    for process_inst, h in _hists.items():
                        if process_inst.id in merged_hists:
                            merged_hists[process_inst.id] += h
                        else:
                            merged_hists[process_inst.id] = h
                            process_memory[process_inst.id] = process_inst

                process_insts = list(process_memory.values())
                hists = {process_memory[process_id]: h for process_id, h in merged_hists.items()}
            else:
                hists = hists[self.config_inst]
                process_insts = list(hists.keys())

            # axis selections and reductions
            _hists = OrderedDict()
            for process_inst in hists.keys():
                h = hists[process_inst]
                # determine expected shifts from the intersection of requested shifts and those known for the process
                process_shifts = (
                    process_shift_map[process_inst.name]
                    if process_inst.name in process_shift_map
                    else {"nominal"}
                )
                expected_shifts = plot_shift_names & process_shifts
                if not expected_shifts:
                    raise Exception(f"no shifts to plot found for process {process_inst.name}")
                # selections
                h = h[{
                    "category": [
                        hist.loc(c.name)
                        for c in leaf_category_insts
                        if c.name in h.axes["category"]
                    ],
                    "shift": [
                        hist.loc(s_name)
                        for s_name in expected_shifts
                        if s_name in h.axes["shift"]
                    ],
                }]
                # reductions
                h = h[{"category": sum}]
                # store
                _hists[process_inst] = h
            hists = _hists

            # copy process instances once so that their auxiliary data fields can be used as a storage
            # for process-specific plot parameters later on in plot scripts without affecting the
            # original instances
            fake_root = od.Process(
                name=f"{hex(id(object()))[2:]}",
                id="+",
                processes=list(hists.keys()),
            ).copy()
            process_insts = list(fake_root.processes)
            fake_root.processes.clear()
            hists = dict(zip(process_insts, hists.values()))

            # correct luminosity label in case of multiple configs
            if len(self.config_insts) == 1:
                config_inst = self.config_inst
            else:
                config_inst = self.config_insts[0].copy(id=-1, name=f"{self.config_insts[0].name}_merged")
                config_inst.x.luminosity = sum([_config_inst.x.luminosity for _config_inst in self.config_insts])

            # call the plot function
            fig, weights = self.call_plot_func(
                self.plot_function,
                hists=hists,
                config_inst=config_inst,
                category_inst=category_inst.copy_shallow(),
                variable_insts=[var_inst.copy_shallow() for var_inst in variable_insts],
                shift_insts=plot_shifts,
                **self.get_plot_parameters(),
            )

            # save the plot
            for outp in self.output()["plots"]:
                outp.dump(fig[0], formatter="mpl")

            for outp in self.output()["weights"]:
                outp.dump(weights.json(exclude_unset=False), formatter="gzip", mode="wt")


class ProduceTriggerWeightBaseSingleShift(
    ShiftTask,
    ProduceTriggerWeightBase,
):
    # use the MergeHistograms task to trigger upstream TaskArrayFunction initialization
    resolution_task_cls = MergeHistograms
    exclude_index = True

    reqs = Requirements(
        ProduceTriggerWeightBase.reqs,
        MergeHistograms=MergeHistograms,
    )

    def create_branch_map(self):
        return [
            DotDict({"category": cat_name, "variable": var_name})
            for var_name in sorted(self.variables)
            for cat_name in sorted(self.categories)
        ]

    def workflow_requires(self):
        reqs = super().workflow_requires()
        return reqs

    def requires(self):
        req = {}

        for i, config_inst in enumerate(self.config_insts):
            sub_datasets = self.datasets[i]
            req[config_inst.name] = {}
            for d in sub_datasets:
                if d in config_inst.datasets.names():
                    req[config_inst.name][d] = self.reqs.MergeHistograms.req(
                        self,
                        config=config_inst.name,
                        shift=self.global_shift_insts[config_inst].name,
                        dataset=d,
                        branch=-1,
                        _exclude={"branches"},
                        _prefer_cli={"variables"},
                    )
        return req

    def plot_parts(self) -> law.util.InsertableDict:
        parts = super().plot_parts()

        parts["processes"] = f"proc_{self.processes_repr}"
        parts["category"] = f"cat_{self.branch_data.category}"
        parts["variable"] = f"var_{self.branch_data.variable}"

        hooks_repr = self.hist_hooks_repr
        if hooks_repr:
            parts["hook"] = f"hooks_{hooks_repr}"

        return parts

    def output(self):
        return {
            "plots": [self.target(name) for name in self.get_plot_names("plot")],
            "weights": [self.target(name.replace(".pdf", ".json.gz")) for name in self.get_plot_names("weight")],
        }

    def store_parts(self) -> law.util.InsertableDict:
        parts = super().store_parts()
        if "shift" in parts:
            parts.insert_before("datasets", "shift", parts.pop("shift"))
        return parts

    def get_plot_shifts(self):
        return [get_shift_from_configs(self.config_insts, self.shift)]


class ProduceTriggerWeight(
    ProduceTriggerWeightBaseSingleShift,
    PlotBase1D,
):
    plot_function = PlotBase.plot_function.copy(
        default="alljets.plotting.trigger_eff_closure_1D.produce_trig_weight",
        add_default_to_description=True,
    )


class ProduceTriggerWeightPerConfig(
    ProduceTriggerWeight,
    law.WrapperTask,
):
    # force this one to be a local workflow
    workflow = "local"
    output_collection_cls = law.NestedSiblingFileCollection

    def requires(self):
        return {
            config: ProduceTriggerWeight.req(
                self,
                datasets=(self.datasets[i],),
                processes=(self.processes[i],),
                configs=(config,),
            )
            for i, config in enumerate(self.configs)
        }
