import os
import json
import random
import numpy as np
import pytest
import warnings

from pybullet_planning import INF
from pybullet_planning import load_pybullet, connect, wait_for_user, LockRenderer, has_gui, WorldSaver, HideOutput, \
    reset_simulation, disconnect
from pybullet_planning import interpolate_poses, multiply, unit_pose, get_relative_pose
from pybullet_planning import interval_generator, sample_tool_ik
from pybullet_planning import Pose, Point, Euler, unit_pose
from pybullet_planning import joints_from_names, link_from_name, has_link, get_collision_fn, get_disabled_collisions, \
    draw_pose, set_pose, set_joint_positions, dump_body, dump_world, get_body_body_disabled_collisions, create_obj, \
    get_link_pose, clone_body

from pychoreo.process_model.cartesian_process import CartesianProcess, CartesianSubProcess
from pychoreo.process_model.trajectory import Trajectory, MotionTrajectory
from pychoreo.process_model.gen_fn import CartesianPoseGenFn
from pychoreo.utils.stream_utils import get_random_direction_generator, get_enumeration_pose_generator
from pychoreo.utils.parsing_utils import export_trajectory
from pychoreo.cartesian_planner.ladder_graph_interface import solve_ladder_graph_from_cartesian_process_list
from pychoreo.cartesian_planner.sparse_ladder_graph import SparseLadderGraph

import pychoreo_examples
from pychoreo_examples.extrusion.parsing import load_extrusion, create_elements_bodies, parse_saved_trajectory, \
    parse_feasible_ee_maps, save_feasible_ee_maps
from pychoreo_examples.extrusion.visualization import set_extrusion_camera, draw_extrusion_sequence, display_trajectories
from pychoreo_examples.extrusion.stream import get_extrusion_ee_pose_compose_fn, get_ee_pose_enumerate_map_fn, \
    build_extrusion_cartesian_process_sequence
from pychoreo_examples.extrusion.utils import max_valence_extrusion_direction_routing
from pychoreo_examples.extrusion.transition_planner import solve_transition_between_extrusion_processes

import ikfast_kuka_kr6_r900

def load_extrusion_end_effector(ee_urdf_path):
    with HideOutput():
        ee = load_pybullet(ee_urdf_path)
    return ee

def build_extrusion_cartesian_process(elements, node_points, robot, sample_ik_fn, ik_joint_names, base_link_name, extrusion_end_effector, tool_from_root=None, viz_step=False):
    # load EE body, for debugging purpose
    orig_ee_body = load_extrusion_end_effector(extrusion_end_effector)
    ee_body = clone_body(orig_ee_body, visual=False)
    ik_joints = joints_from_names(robot, ik_joint_names)

    cart_traj_dict = {}
    for element in elements:
        process_name = 'extrusion-E{}'.format(element)
        element = tuple(element)
        n1, n2 = element
        path_pts = [node_points[n1], node_points[n2]]

        # an example for EE pose random generation, yaw (rotation around the direction axis) is set to 0
        random_dir_gen = get_random_direction_generator()
        ee_pose_gen_fn = CartesianPoseGenFn(random_dir_gen,
                                            get_extrusion_ee_pose_compose_fn(interpolate_poses, approach_distance=0.01, pos_step_size=0.003),
                                            base_path_pts=path_pts)

        # build three sub-processes: approach, extrusion, retreat
        extrusion_sub_procs = [CartesianSubProcess(sub_process_name='approach-extrude'),
                               CartesianSubProcess(sub_process_name='extrude'),
                               CartesianSubProcess(sub_process_name='extrude-retreat')]

        cart_process = CartesianProcess(process_name=process_name,
            robot=robot, ik_joint_names=ik_joint_names,
            sub_process_list=extrusion_sub_procs,
            ee_pose_gen_fn=ee_pose_gen_fn, sample_ik_fn=sample_ik_fn,
            element_identifier=element)

        ee_poses = cart_process.sample_ee_poses()
        for sp_id, sp in enumerate(ee_poses):
            # print('E #{} - sub process #{}'.format(element, sp_id))
            for ee_p in sp:
                yaw = random.uniform(-np.pi, np.pi)
                ee_p = multiply(ee_p, Pose(euler=Euler(yaw=yaw)), tool_from_root)
                set_pose(ee_body, ee_p)
                if has_gui(): wait_for_user()

        # this should raise an not implemented error since we haven't specify the collision function yet
        for sp in cart_process.sub_process_list:
            with pytest.raises(NotImplementedError):
                conf = [0] * 6
                sp.collision_fn(conf)

        ik_sols = cart_process.get_ik_sols(ee_poses, check_collision=False)
        for sp_id, sp_jt_sols in enumerate(ik_sols):
            for jt_sols in sp_jt_sols:
                for jts in jt_sols:
                    set_joint_positions(robot, ik_joints, jts)
                    if has_gui(): wait_for_user()

        cart_traj_dict[element] = cart_process
    return cart_traj_dict

@pytest.mark.extrusion
# @pytest.mark.parametrize('solve_method', [('sparse_ladder_graph')])
# @pytest.mark.parametrize('solve_method', [('ladder_graph')])
@pytest.mark.parametrize('solve_method', [('ladder_graph'), ('sparse_ladder_graph')])
def test_extrusion_ladder_graph(viewer, extrusion_problem_path, extrusion_robot_data, extrusion_end_effector, solve_method):
    sample_time = 30
    sparse_time_out = 5 # 900
    roll_disc = 10 # 60
    pitch_disc = 10
    yaw_sample_size = 5 if solve_method == 'ladder_graph' else INF
    linear_step_size = 0.003 # m
    jt_res = 0.1 # 0.01
    radius = 1e-6 # 0.002
    shrink = 0.01 # m
    RRT_RESTARTS = 5
    RRT_ITERATIONS = 40

    # * create robot and pb environment
    (robot_urdf, base_link_name, tool_root_link_name, ee_link_name, ik_joint_names, disabled_self_collision_link_names), \
        (workspace_urdf, workspace_robot_disabled_link_names) = extrusion_robot_data
    connect(use_gui=viewer)
    with HideOutput():
        robot = load_pybullet(robot_urdf, fixed_base=True)
        workspace = load_pybullet(workspace_urdf, fixed_base=True)
    ik_fn = ikfast_kuka_kr6_r900.get_ik
    ik_joints = joints_from_names(robot, ik_joint_names)

    # * printout bodies in the scene information
    # dump_world()

    initial_conf = [0.08, -1.57, 1.74, 0.08, 0.17, -0.08]
    set_joint_positions(robot, ik_joints, initial_conf)

    # * get tool TCP from flange (ee_link) transformation
    # this is for the robot that has end effector specified in its URDF
    root_link = link_from_name(robot, tool_root_link_name)
    tool_link = link_from_name(robot, ee_link_name)
    tool_from_root = get_relative_pose(robot, root_link, tool_link)

    if has_gui() :
        tcp_pose = get_link_pose(robot, tool_link)
        draw_pose(tcp_pose)
        # wait_for_user()

    # * specify ik fn wrapper
    def get_sample_ik_fn(robot, ik_fn, ik_joint_names, base_link_name, tool_from_root=None):
        def sample_ik_fn(world_from_tcp):
            if tool_from_root:
                world_from_tcp = multiply(world_from_tcp, tool_from_root)
            return sample_tool_ik(ik_fn, robot, ik_joint_names, base_link_name, world_from_tcp, get_all=True)
        return sample_ik_fn
    # ik generation function stays the same for all cartesian processes
    sample_ik_fn = get_sample_ik_fn(robot, ik_fn, ik_joint_names, base_link_name, tool_from_root)

    # * get problem & pre-computed json file paths
    file_path, seq_file_path, ee_maps_file_path, _, = extrusion_problem_path

    # * load shape (get nodal positions)
    elements, node_points, ground_nodes = load_extrusion(file_path)
    assert all(isinstance(e, tuple) and len(e) == 2 for e in elements)
    assert all(isinstance(pt, np.ndarray) and len(pt) == 3 for pt in node_points)
    assert all(isinstance(gn, int) for gn in ground_nodes)

    # * create element bodies (collision geometries)
    with LockRenderer():
        draw_pose(unit_pose(), length=1.)
        element_bodies = dict(zip(elements,
            create_elements_bodies(node_points, elements, radius=radius, shrink=shrink)))
        assert all(isinstance(e_body, int) for e_body in element_bodies.values())
        set_extrusion_camera(node_points)

    # * create cartesian processes without a sequence being given, with random pose generators
    # this is just a demonstration to help us do some sanity check with visualization
    # with WorldSaver():
    #     _ = build_extrusion_cartesian_process(elements, node_points, robot, sample_ik_fn, ik_joint_names,
    #             base_link_name, extrusion_end_effector, tool_from_root, viz_step=True)

    # * load precomputed sequence
    parsed_ee_fmaps = None
    ee_pose_map_fn = None
    try:
        parsed_ee_fmaps, ee_pose_map_fn = parse_feasible_ee_maps(ee_maps_file_path)
        if parsed_ee_fmaps is None:
            with open(seq_file_path, 'r') as f:
                seq_data = json.loads(f.read())
            print('Precomputed sequence loaded: ', seq_file_path)
            element_sequence = [tuple(e) for e in seq_data['plan']]
        else:
            print('Precomputed sequence and feasible_ee_maps loaded.')
            element_sequence = [tuple(e) for e in parsed_ee_fmaps.keys()]
    except:
        warnings.warn('Parsing precomputed sequence file failed - using default element sequence.')
        element_sequence = [tuple(e) for e in elements]
    assert all(isinstance(e, tuple) and len(e) == 2 for e in element_sequence)

    # * compute reverse flags based on the precomputed sequence
    reverse_flags = max_valence_extrusion_direction_routing(element_sequence, elements, node_points, ground_nodes)

    # * construct ignored body-body links for collision checking
    # in this case, including self-collision between links of the robot
    disabled_self_collisions = get_disabled_collisions(robot, disabled_self_collision_link_names)
    # and links between the robot and the workspace (e.g. robot_base_link to base_plate)
    extra_disabled_collisions = get_body_body_disabled_collisions(robot, workspace, workspace_robot_disabled_link_names)

    ee_body = load_extrusion_end_effector(extrusion_end_effector)

    # * building collision function based on the given sequence
    with LockRenderer(False):
        cart_process_seq, e_fmaps = build_extrusion_cartesian_process_sequence(
            element_sequence, element_bodies, node_points, ground_nodes,
            robot, ik_joint_names, sample_ik_fn, ee_body,
            ee_fmaps=parsed_ee_fmaps, ee_pose_map_fn=ee_pose_map_fn,
            roll_disc=roll_disc, pitch_disc=pitch_disc, yaw_sample_size=yaw_sample_size, sample_time=sample_time,
            linear_step_size=linear_step_size, tool_from_root=tool_from_root,
            self_collisions=True, disabled_collisions=disabled_self_collisions,
            obstacles=[workspace], extra_disabled_collisions=extra_disabled_collisions,
            reverse_flags=reverse_flags, verbose=True)

        here = os.path.dirname(__file__)
        save_dir = os.path.join(here, 'test_data')
        save_feasible_ee_maps(e_fmaps, roll_disc, pitch_disc, save_dir, overwrite=True, shape_file_path=file_path, indent=None)

        # sanity check
        exist_nonfeasible = False
        for element, fmap in e_fmaps.items():
            if sum(fmap) == 0:
                exist_nonfeasible = True
                print('E#{} feasible map empty, precomputed sequence should have a feasible ee pose range!'.format(element))
        assert not exist_nonfeasible

    assert isinstance(cart_process_seq, list)
    assert all(isinstance(cp, CartesianProcess) for cp in cart_process_seq)
    assert all([cart.element_identifier == e for cart, e in zip(cart_process_seq, element_sequence)])

    # * draw the pruned EE direction set
    if has_gui():
        # just move the element bodies and ee_body away to clear the visualized scene
        set_pose(ee_body, unit_pose())
        if not ee_pose_map_fn:
            ee_pose_map_fn = get_ee_pose_enumerate_map_fn(roll_disc, pitch_disc)
        for e_body in element_bodies.values(): set_pose(e_body, unit_pose())
        draw_extrusion_sequence(node_points, element_bodies, element_sequence, e_fmaps, ee_pose_map_fn=ee_pose_map_fn,
                                line_width=5, direction_len=0.005, time_step=INF)

    viz_inspect = False
    with LockRenderer(not viz_inspect):
        if solve_method == 'ladder_graph':
            print('\n'+'#' * 10)
            print('Solving with the vanilla ladder graph search algorithm.')
            cart_process_seq = solve_ladder_graph_from_cartesian_process_list(cart_process_seq,
                verbose=True, warning_pause=False, viz_inspect=viz_inspect, check_collision=True)
        elif solve_method == 'sparse_ladder_graph':
            print('\n'+'#' * 10)
            print('Solving with the sparse ladder graph search algorithm.')
            sparse_graph = SparseLadderGraph(cart_process_seq)
            sparse_graph.find_sparse_path(verbose=True, vert_timeout=sample_time, sparse_sample_timeout=sparse_time_out)
            cart_process_seq = sparse_graph.extract_solution(verbose=True)
        else:
            raise ValueError('Invalid solve method!')
        assert all(isinstance(cp, CartesianProcess) for cp in cart_process_seq)

        # * extract trajectory from CartProcesses and add tags
        print_trajs = [[] for _ in range(len(cart_process_seq))]
        for cp_id, cp in enumerate(cart_process_seq):
            for sp_id, sp in enumerate(cp.sub_process_list):
                assert sp.trajectory, '{}-{} does not have a Cartesian plan found!'.format(cp, sp)
                print_trajs[cp_id].append(sp.trajectory)
                print(sp.trajectory)
    full_trajs = print_trajs

    # * transition motion planning between extrusions
    return2idle = True
    transition_traj = solve_transition_between_extrusion_processes(robot, ik_joints, print_trajs, element_bodies, initial_conf,
                                                                   disabled_collisions=disabled_self_collisions,
                                                                   obstacles=[workspace], return2idle=return2idle,
                                                                   resolutions=[jt_res]*len(ik_joints),
                                                                   restarts=RRT_RESTARTS, iterations=RRT_ITERATIONS)
    assert all(isinstance(tt, MotionTrajectory) for tt in transition_traj)
    if return2idle:
        transition_traj[-1].tag = 'return2idle'
        assert len(transition_traj)-1 == len(print_trajs)
    else:
        assert len(transition_traj) == len(print_trajs)

    # * weave the Cartesian and transition processses together
    for cp_id, print_trajs in enumerate(full_trajs):
        print_trajs.insert(0, transition_traj[cp_id])
    if return2idle:
        full_trajs[-1].append(transition_traj[-1])

    here = os.path.dirname(__file__)
    save_dir = os.path.join(here, 'results')
    export_trajectory(save_dir, full_trajs, ee_link_name, indent=None, shape_file_path=file_path, include_robot_data=False, include_link_path=True)

    # * disconnect and close pybullet engine used for planning, visualizing trajectories will start a new one
    reset_simulation()
    disconnect()

    # visualize plan
    if viewer:
        display_trajectories(robot_urdf, ik_joint_names, ee_link_name, node_points, ground_nodes, full_trajs,
                             workspace_urdf=workspace_urdf, animate=True, cart_time_step=0.02, tr_time_step=0.05)

@pytest.mark.extrusion_resolve_trans
def test_resolve_trans(viewer, extrusion_problem_path, extrusion_robot_data):
    jt_res = 0.01 # 0.01
    shrink = 0.00 # m
    RRT_RESTARTS = 5
    RRT_ITERATIONS = 40

    # * create robot and pb environment
    (robot_urdf, base_link_name, tool_root_link_name, ee_link_name, ik_joint_names, disabled_self_collision_link_names), \
        (workspace_urdf, workspace_robot_disabled_link_names) = extrusion_robot_data

    # * get problem & pre-computed json file paths
    file_path, _, _, result_file_name = extrusion_problem_path

    # * load shape (get nodal positions)
    elements, node_points, ground_nodes = load_extrusion(file_path)

    # * parse saved trajectory results
    here = os.path.dirname(__file__)
    save_file_path = os.path.join(here, 'results', result_file_name)

    connect(use_gui=False)
    with HideOutput():
        robot = load_pybullet(robot_urdf, fixed_base=True)
        workspace = load_pybullet(workspace_urdf, fixed_base=True)
    ik_joints = joints_from_names(robot, ik_joint_names)
    initial_conf = [0.08, -1.57, 1.74, 0.08, 0.17, -0.08]
    disabled_self_collisions = get_disabled_collisions(robot, disabled_self_collision_link_names)
    extra_disabled_collisions = get_body_body_disabled_collisions(robot, workspace, workspace_robot_disabled_link_names)

    # * create element bodies (collision geometries)
    with LockRenderer():
        element_bodies = dict(zip(elements,
            create_elements_bodies(node_points, elements, radius=0.002, shrink=shrink)))
        assert all(isinstance(e_body, int) for e_body in element_bodies.values())
        set_extrusion_camera(node_points)

    # * parse saved trajectory
    old_full_trajs = parse_saved_trajectory(save_file_path)
    print_trajs = []
    for cp_id, cp_trajs in enumerate(old_full_trajs):
        cp_print_trajs = []
        for trajectory in cp_trajs:
            if not isinstance(trajectory, MotionTrajectory):
                cp_print_trajs.append(trajectory)
        print_trajs.append(cp_print_trajs)
    full_trajs = print_trajs

    # * transition motion planning between extrusions
    return2idle = True
    transition_traj = solve_transition_between_extrusion_processes(robot, ik_joints, print_trajs, element_bodies, initial_conf,
                                                                   disabled_collisions=disabled_self_collisions,
                                                                   obstacles=[workspace], return2idle=return2idle,
                                                                   resolutions=[jt_res]*len(ik_joints),
                                                                   restarts=RRT_RESTARTS, iterations=RRT_ITERATIONS)
    assert all(isinstance(tt, MotionTrajectory) for tt in transition_traj)
    if return2idle:
        transition_traj[-1].tag = 'return2idle'
        assert len(transition_traj)-1 == len(print_trajs)
    else:
        assert len(transition_traj) == len(print_trajs)

    # * weave the Cartesian and transition processses together
    for cp_id, ctrajs in enumerate(full_trajs):
        ctrajs.insert(0, transition_traj[cp_id])
    if return2idle:
        full_trajs[-1].append(transition_traj[-1])

    here = os.path.dirname(__file__)
    save_dir = os.path.join(here, 'results')
    export_trajectory(save_dir, full_trajs, ee_link_name,
        indent=None, shape_file_path=file_path, include_robot_data=False, include_link_path=True)

    # * disconnect and close pybullet engine used for planning, visualizing trajectories will start a new one
    reset_simulation()
    disconnect()

    # visualize plan
    if viewer:
        display_trajectories(robot_urdf, ik_joint_names, ee_link_name, node_points, ground_nodes, full_trajs,
                             workspace_urdf=workspace_urdf, animate=True, cart_time_step=0.07, tr_time_step=0.01)

@pytest.mark.extrusion_viz
def test_parse_and_visualize_results(viewer, extrusion_problem_path, extrusion_robot_data, extrusion_end_effector):
    # * create robot and pb environment
    (robot_urdf, base_link_name, tool_root_link_name, ee_link_name, ik_joint_names, disabled_self_collision_link_names), \
        (workspace_urdf, workspace_robot_disabled_link_names) = extrusion_robot_data

    # * get problem & pre-computed json file paths
    file_path, _, _, result_file_name = extrusion_problem_path

    # * load shape (get nodal positions)
    _, node_points, ground_nodes = load_extrusion(file_path)

    # * parse saved trajectory results
    here = os.path.dirname(__file__)
    save_file_path = os.path.join(here, 'results', result_file_name)

    # parse without connect
    with pytest.warns(UserWarning, match='Pybullet environment not connected*'):
        full_trajs = parse_saved_trajectory(save_file_path)

    # parse with connect but robot body not added
    connect(use_gui=False)
    with pytest.raises(ValueError):
        full_trajs = parse_saved_trajectory(save_file_path)
    disconnect()

    # parse with connect
    connect(use_gui=False)
    with HideOutput():
        robot = load_pybullet(robot_urdf, fixed_base=True)
    full_trajs = parse_saved_trajectory(save_file_path)
    disconnect()

    # visualize plan
    if viewer:
        display_trajectories(robot_urdf, ik_joint_names, ee_link_name, node_points, ground_nodes, full_trajs,
                             workspace_urdf=workspace_urdf, animate=True, cart_time_step=0.07, tr_time_step=0.01)
